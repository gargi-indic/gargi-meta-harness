import json

from typer.testing import CliRunner

from harness.cli import app

runner = CliRunner()


def test_cli_validate_success():
    import importlib.resources

    pb_path = importlib.resources.files("harness.playbooks").joinpath("plan-build-review.yaml")

    result = runner.invoke(app, ["playbook", "validate", str(pb_path)])
    assert result.exit_code == 0
    assert "ok: plan-build-review" in result.stdout


def test_cli_validate_failure(tmp_path):
    bad_pb = tmp_path / "bad.yaml"
    bad_pb.write_text(
        "apiVersion: harness/v1\nid: bad\nnodes:\n  n1: {kind: escalate}\nedges:\n  'n1.bad': 'n1'"
    )

    result = runner.invoke(app, ["playbook", "validate", str(bad_pb)])
    assert result.exit_code == 1


def test_cli_schema():
    result = runner.invoke(app, ["playbook", "schema"])
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert "Playbook" in schema["title"]


def test_cli_list():
    result = runner.invoke(app, ["playbook", "list"])
    assert result.exit_code == 0
    assert "plan-build-review" in result.stdout
