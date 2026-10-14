"""Tests for the CLI entrypoint."""

from typer.testing import CliRunner

from harness import __version__
from harness.cli import app

runner = CliRunner()


def test_version() -> None:
    """Test the --version flag."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_status() -> None:
    """Test the status command placeholder."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "not configured"
