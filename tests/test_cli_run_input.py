"""``harness run --input KEY=VALUE`` (M4.7): parsed, listed in the help, and handed to the new run."""

import re
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from harness import cli
from harness.cli import app, parse_inputs
from harness.playbook.load import builtin_playbooks
from harness.store.db import Store
from tests.engine.helpers import make_issue

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_pairs_become_a_dict_and_only_the_first_equals_sign_splits() -> None:
    assert parse_inputs([]) == {}
    assert parse_inputs(["plan_path=docs/PLAN.md", "note=a=b", "empty="]) == {
        "plan_path": "docs/PLAN.md",
        "note": "a=b",
        "empty": "",
    }
    assert parse_inputs(["k=1", "k=2"]) == {"k": "2"}  # the last one wins


@pytest.mark.parametrize("bad", ["plan_path", "=x", "  =x"])
def test_a_pair_without_a_key_or_equals_sign_is_refused(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as exc:
        parse_inputs([bad])
    assert exc.value.exit_code == 1
    assert "must look like KEY=VALUE" in capsys.readouterr().out


def test_the_help_documents_the_option() -> None:
    result = runner.invoke(app, ["run", "--help"])
    text = " ".join(ANSI.sub("", result.output).replace("│", " ").split())
    assert result.exit_code == 0
    assert "--input" in text and "KEY=VALUE" in text and "plan_path=docs/PLAN.md" in text


def test_the_command_hands_the_parsed_inputs_to_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(application, repo, number, playbook, agent, max_ticks, inputs):
        seen.update(repo=repo, number=number, playbook=playbook, inputs=inputs)
        return 0

    monkeypatch.setattr(cli, "_build", lambda **kw: object())
    monkeypatch.setattr(cli, "_run_command", fake_run)
    args = [
        "run",
        "o/r",
        "5",
        "--playbook",
        "decompose-epic",
        "--input",
        "plan_path=docs/X.md",
        "--input",
        "a=b",
    ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen == {
        "repo": "o/r",
        "number": 5,
        "playbook": "decompose-epic",
        "inputs": {"plan_path": "docs/X.md", "a": "b"},
    }
    seen.clear()
    assert runner.invoke(app, ["run", "o/r", "5"]).exit_code == 0 and seen["inputs"] == {}
    assert runner.invoke(app, ["run", "o/r", "5", "--input", "oops"]).exit_code == 1


async def test_a_new_run_gets_the_inputs_and_a_continued_one_ignores_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[dict[str, Any]] = []

    def start_run(repo, issue, playbook, **kwargs):
        started.append({"playbook": playbook.id, **kwargs})
        return "o/r#7@1"

    async def resolve(application, repo, playbook_id, ref):
        return builtin_playbooks()[playbook_id], None

    store = Store(":memory:")
    store.migrate()
    application = SimpleNamespace(
        store=store,
        repo_configs={},
        adapters=SimpleNamespace(names=lambda: ["claude"]),
        runner=SimpleNamespace(start_run=start_run),
    )
    monkeypatch.setattr(cli, "_resolve_playbook", resolve)
    issue = make_issue(7)
    run_id = await cli._start_or_continue(
        application, "o/r", 7, "decompose-epic", "claude", issue, {"plan_path": "docs/PLAN.md"}
    )
    assert run_id == "o/r#7@1"
    assert started == [
        {
            "playbook": "decompose-epic",
            "playbook_sha": None,
            "agent": "claude",
            "inputs": {"plan_path": "docs/PLAN.md"},
        }
    ]

    existing = SimpleNamespace(id="o/r#7@1")
    monkeypatch.setattr(cli, "_active_run", lambda application, repo, number: existing)
    await cli._start_or_continue(application, "o/r", 7, None, None, issue, {"plan_path": "x"})
    out = capsys.readouterr().out
    assert "continuing o/r#7@1" in out and "--input is ignored" in out
    assert len(started) == 1
    store.close()
