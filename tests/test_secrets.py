import os
import subprocess
from unittest.mock import patch

import pytest

from harness.secrets import (
    SECRET_NAMES,
    github_token,
    load_env_files,
    parse_env_file,
    redact,
)


def test_parse_env_file():
    text = """
# comment
KEY1=value1
export KEY2="value2"
KEY3='value3'

KEY4 = value4
KEY5=abc # note
KEY6="a#b"
invalid-key=val
123key=val
"""
    result = parse_env_file(text)
    assert result == {
        "KEY1": "value1",
        "KEY2": "value2",
        "KEY3": "value3",
        "KEY4": "value4",
        "KEY5": "abc",
        "KEY6": "a#b",
    }


def test_load_env_files(tmp_path, monkeypatch, caplog):
    file1 = tmp_path / "env1"
    file2 = tmp_path / "env2"
    file1.write_text("VAR1=val1\nVAR2=val2\nVAR_EMPTY_FILE=\n")
    file2.write_text("VAR2=val2_new\nVAR3=val3")

    # Mock expanduser to point to tmp_path files if we were using ~

    monkeypatch.setenv("VAR1", "existing_val1")
    monkeypatch.setenv("VAR_EMPTY_EXISTING", "")
    file1.write_text(file1.read_text() + "VAR_EMPTY_EXISTING=new_val\n")

    # Use load_env_files
    loaded_keys = load_env_files([file1, file2])

    # VAR1 was already set, so it shouldn't be loaded/overwritten
    # VAR2 is set from file1, file2's VAR2 is ignored since it's already set by file1

    assert os.environ["VAR1"] == "existing_val1"
    assert os.environ["VAR2"] == "val2"
    assert os.environ["VAR3"] == "val3"
    assert os.environ["VAR_EMPTY_EXISTING"] == "new_val"
    assert "VAR_EMPTY_FILE" not in os.environ or os.environ["VAR_EMPTY_FILE"] == ""

    assert loaded_keys == ["VAR2", "VAR_EMPTY_EXISTING", "VAR3"]

    # Check that values are never in returned list
    assert "val1" not in str(loaded_keys)
    assert "val2" not in str(loaded_keys)
    assert "val3" not in str(loaded_keys)

    # Check that values are not in logs
    assert "val1" not in caplog.text
    assert "val2" not in caplog.text
    assert "val3" not in caplog.text


def test_github_token_env_vars(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh_token_value")
    assert github_token() == "gh_token_value"

    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "github_token_value")
    assert github_token() == "github_token_value"

    # test empty vars
    monkeypatch.setenv("GH_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    with pytest.raises(RuntimeError):
        github_token()


@patch("subprocess.run")
def test_github_token_subprocess(mock_run, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class MockResult:
        stdout = " subprocess_token \n"

    mock_run.return_value = MockResult()

    assert github_token() == "subprocess_token"
    mock_run.assert_called_once_with(["gh", "auth", "token"], capture_output=True, text=True, check=True)


@patch("subprocess.run")
def test_github_token_subprocess_empty(mock_run, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    class MockResult:
        stdout = " \n"

    mock_run.return_value = MockResult()

    with pytest.raises(RuntimeError):
        github_token()


@patch("subprocess.run")
def test_github_token_failure(mock_run, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    mock_run.side_effect = subprocess.CalledProcessError(1, ["gh"])

    with pytest.raises(RuntimeError) as excinfo:
        github_token()

    assert "no GitHub token" in str(excinfo.value)


def test_redact(monkeypatch):
    for name in SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("GH_TOKEN", "secret_gh_token")
    monkeypatch.setenv("JULES_API_KEY", "short")  # < 8 chars, should not be redacted

    text = "Here is my secret_gh_token and short key and extra_secret"

    # test with extra
    redacted_text = redact(text, extra=["extra_secret"])

    assert "secret_gh_token" not in redacted_text
    assert "extra_secret" not in redacted_text
    assert "short" in redacted_text
    assert redacted_text == "Here is my [redacted] and short key and [redacted]"
