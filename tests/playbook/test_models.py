import pytest

from harness.playbook.models import duration_seconds


def test_duration_seconds():
    assert duration_seconds("5s") == 5
    assert duration_seconds("2m") == 120
    assert duration_seconds("1h") == 3600


def test_duration_parsing_error():
    with pytest.raises(ValueError):
        duration_seconds("5x")
