"""CLI: gates, approve, reject, pause, resume, cancel, retry (M1.6). Uses a temp config and database."""

import asyncio
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harness import config as config_module
from harness.adapters.base import AdapterRegistry
from harness.cli import app
from harness.config import GlobalConfig, RepoEntry, save_global
from harness.engine import Done, ExecutorRegistry, Runner
from harness.engine.executors.gate import GateExecutor
from harness.store.db import Store
from harness.store.repo import get_gate, get_run
from tests.engine.helpers import REPO, FakeClock, Scripted, make_issue, make_playbook

cli = CliRunner()

PLAYBOOK = make_playbook(
    {"a": {"kind": "check", "check": "command", "command": "x"}, "g": {"kind": "gate", "gate": "g"}},
    {"a.fail": "g", "a.pass": "@done", "g.approved": "@done", "g.rejected": "@failed"},
    id="cli-gates",
).model_copy(update={"gates": {"g": "human"}})


@dataclass
class World:
    db_path: Path
    run_id: str
    gate_id: str

    def store(self) -> Store:
        store = Store(self.db_path)
        store.migrate()
        return store


def build_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repos: list[str]) -> World:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    cfg = GlobalConfig(
        data_dir=tmp_path / "data", env_files=[], repos=[RepoEntry(name=name) for name in repos]
    )
    save_global(cfg)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    clock = FakeClock()
    store = Store(cfg.db_path, clock=lambda: datetime.fromtimestamp(clock.t, UTC))
    store.migrate()
    registry = ExecutorRegistry()
    registry.register("gate", GateExecutor())
    registry.register("check", Scripted({"a": [Done("fail", {"error": "boom"})]}))
    runner = Runner(store, None, registry, AdapterRegistry(), clock=clock)
    run_id = runner.start_run(REPO, make_issue(7), PLAYBOOK)

    async def drive() -> None:
        await runner.tick()
        clock.advance(60)
        await runner.tick()

    asyncio.run(drive())
    store.close()
    return World(cfg.db_path, run_id, f"{run_id}/g/1/gate")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    yield build_world(tmp_path, monkeypatch, [REPO])


def run_status(world: World) -> str:
    store = world.store()
    try:
        run = get_run(store, world.run_id)
        assert run is not None
        return run.status
    finally:
        store.close()


def test_gates_lists_pending(world: World) -> None:
    result = cli.invoke(app, ["gates"])
    assert result.exit_code == 0, result.output
    line = next(ln for ln in result.stdout.splitlines() if world.gate_id in ln)
    assert world.run_id in line and "#7" in line and " g " in line + " "


def test_gates_repo_filter(world: World) -> None:
    result = cli.invoke(app, ["gates", "--repo", "x/y"])
    assert result.exit_code == 0
    assert world.gate_id not in result.stdout
    result = cli.invoke(app, ["gates", "--repo", REPO])
    assert world.gate_id in result.stdout


def test_gates_hides_decided(world: World) -> None:
    assert cli.invoke(app, ["approve", world.gate_id]).exit_code == 0
    assert world.gate_id not in cli.invoke(app, ["gates"]).stdout


def test_approve_by_gate_id(world: World) -> None:
    result = cli.invoke(app, ["approve", world.gate_id, "--note", "lgtm"])
    assert result.exit_code == 0, result.output
    store = world.store()
    gate = get_gate(store, world.gate_id)
    store.close()
    assert gate is not None
    assert (gate.status, gate.note, gate.decided_by) == ("approved", "lgtm", "cli")


@pytest.mark.parametrize("ref", ["7", "#7"])
def test_approve_by_issue(world: World, ref: str) -> None:
    result = cli.invoke(app, ["approve", ref, "--repo", REPO])
    assert result.exit_code == 0, result.output
    store = world.store()
    gate = get_gate(store, world.gate_id)
    store.close()
    assert gate is not None and gate.status == "approved" and gate.note is None


def test_issue_ref_defaults_to_the_only_registered_repo(world: World) -> None:
    assert cli.invoke(app, ["approve", "7"]).exit_code == 0


def test_issue_ref_needs_repo_when_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_world(tmp_path, monkeypatch, [REPO, "o/other"])
    result = cli.invoke(app, ["approve", "7"])
    assert result.exit_code == 1
    assert len(result.stdout.strip().splitlines()) == 1


def test_reject_requires_note(world: World) -> None:
    result = cli.invoke(app, ["reject", world.gate_id])
    assert result.exit_code != 0
    # Rich may colour the usage error (CI sets FORCE_COLOR), splitting "--note" with ANSI codes.
    assert "--note" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    store = world.store()
    gate = get_gate(store, world.gate_id)
    store.close()
    assert gate is not None and gate.status == "pending"


def test_reject_with_note(world: World) -> None:
    result = cli.invoke(app, ["reject", "7", "--repo", REPO, "--note", "wrong approach"])
    assert result.exit_code == 0, result.output
    store = world.store()
    gate = get_gate(store, world.gate_id)
    events = store.query("SELECT data_json FROM events WHERE kind = 'gate_decided'")
    store.close()
    assert gate is not None
    assert (gate.status, gate.note, gate.decided_by) == ("rejected", "wrong approach", "cli")
    assert len(events) == 1


def test_decision_errors_exit_one_with_a_single_line(world: World) -> None:
    for args in (["approve", "no/such/gate"], ["approve", "99", "--repo", REPO]):
        result = cli.invoke(app, args)
        assert result.exit_code == 1
        assert len(result.stdout.strip().splitlines()) == 1
    assert cli.invoke(app, ["approve", world.gate_id]).exit_code == 0
    again = cli.invoke(app, ["approve", world.gate_id])
    assert again.exit_code == 1 and "not pending" in again.stdout


def test_pause_and_resume(world: World) -> None:
    assert cli.invoke(app, ["pause", world.run_id]).exit_code == 0
    assert run_status(world) == "paused"
    result = cli.invoke(app, ["resume", world.run_id])
    assert result.exit_code == 0, result.output
    assert run_status(world) == "needs-you"


def test_resume_to_node(world: World) -> None:
    result = cli.invoke(app, ["resume", world.run_id, "--to", "a"])
    assert result.exit_code == 0, result.output
    assert run_status(world) == "running"
    bad = cli.invoke(app, ["resume", world.run_id, "--to", "nowhere"])
    assert bad.exit_code == 1 and "nowhere" in bad.stdout


def test_cancel(world: World) -> None:
    result = cli.invoke(app, ["cancel", world.run_id])
    assert result.exit_code == 0, result.output
    assert run_status(world) == "cancelled"


def test_retry_defaults_to_the_failed_node(world: World) -> None:
    result = cli.invoke(app, ["retry", world.run_id])
    assert result.exit_code == 0, result.output
    assert "a" in result.stdout
    store = world.store()
    run = get_run(store, world.run_id)
    store.close()
    assert run is not None and run.context["visits"]["a"] == 2


def test_retry_named_node_errors(world: World) -> None:
    result = cli.invoke(app, ["retry", world.run_id, "nowhere"])
    assert result.exit_code == 1 and "nowhere" in result.stdout


def test_controls_on_unknown_run(world: World) -> None:
    for args in (["pause", "nope"], ["resume", "nope"], ["cancel", "nope"], ["retry", "nope"]):
        result = cli.invoke(app, args)
        assert result.exit_code == 1, args
        assert "not found" in result.stdout


def test_controls_enqueue_to_the_outbox_only(world: World) -> None:
    cli.invoke(app, ["pause", world.run_id])
    store = world.store()
    statuses = [r["status"] for r in store.query("SELECT status FROM outbox")]
    store.close()
    assert statuses and set(statuses) == {"pending"}


def test_no_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "CONFIG_HOME", tmp_path / "config")
    save_global(GlobalConfig(data_dir=tmp_path / "empty", env_files=[]))
    for args in (["gates"], ["approve", "x"], ["pause", "r"]):
        result = cli.invoke(app, args)
        assert result.exit_code == 1
        assert "harness sync" in result.stdout
