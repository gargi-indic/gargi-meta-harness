"""``harness costs``: a table of what the runs spent, from the local usage mirror."""

from typer.testing import CliRunner

from harness import config as config_module
from harness.cli import app
from harness.config import GlobalConfig, save_global
from harness.ops.costs import UsageRepo
from harness.store.db import Store
from harness.store.repo import RunRow, insert_run

runner = CliRunner()


def seed_db(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(config_module, "CONFIG_HOME", home / "cfg")
    cfg = GlobalConfig(data_dir=home / "data", env_files=[])
    save_global(cfg)
    cfg.data_path.mkdir(parents=True)
    store = Store(cfg.db_path)
    store.migrate()
    for number, run_id in ((5, "o/r#5@1"), (6, "o/r#6@1")):
        insert_run(
            store,
            RunRow(
                id=run_id,
                repo="o/r",
                issue=number,
                playbook_id="p",
                playbook_sha=None,
                status="done",
                context={},
                ledger_comment_id=None,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            ),
        )
    usage = UsageRepo(store)
    usage.add(
        run_id="o/r#5@1",
        step_id="o/r#5@1/plan/1",
        source="agent",
        adapter="claude",
        cost_usd=1.5,
        created_at="2026-01-01T00:00:00Z",
    )
    usage.add(run_id="o/r#6@1", step_id="o/r#6@1/rev/1", source="review", adapter="reviewer", cost_usd=0.25)
    store.close()


def test_costs_prints_totals_per_run_and_adapter(tmp_path, monkeypatch):
    seed_db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["costs"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any(line.startswith("o/r#5@1") and "#5" in line and line.endswith("1.5000") for line in lines)
    assert any(line.startswith("claude") and line.endswith("1.5000") for line in lines)
    assert any(line.startswith("reviewer") and line.endswith("0.2500") for line in lines)
    assert any(line.startswith("TOTAL") and line.endswith("1.7500") for line in lines)


def test_costs_since_leaves_out_older_spend(tmp_path, monkeypatch):
    seed_db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["costs", "--since", "24h"])  # the 2026-01-01 row is older than a day
    assert result.exit_code == 0, result.output
    assert "o/r#5@1" not in result.output
    assert any(line.startswith("TOTAL") and line.endswith("0.2500") for line in result.output.splitlines())


def test_costs_rejects_a_bad_since(tmp_path, monkeypatch):
    seed_db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["costs", "--since", "yesterday"])
    assert result.exit_code == 1 and "invalid 'since'" in result.output


def test_costs_without_a_database_says_to_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "cfg")
    save_global(GlobalConfig(data_dir=tmp_path / "data", env_files=[]))
    result = runner.invoke(app, ["costs"])
    assert result.exit_code == 1 and "run harness sync" in result.output
