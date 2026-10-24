import pytest
import tempfile
from pathlib import Path
from harness.config import GlobalConfig, load_global, save_global


def test_config_round_trip():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.toml"

        cfg = GlobalConfig()
        cfg.budget.daily_usd = 42.5
        cfg.budget.timezone = "America/New_York"

        save_global(cfg, p)

        loaded = load_global(p)
        assert loaded.budget.daily_usd == 42.5
        assert loaded.budget.timezone == "America/New_York"

        # None values
        cfg.budget.daily_usd = None
        cfg.budget.timezone = None
        save_global(cfg, p)
        loaded2 = load_global(p)
        assert loaded2.budget.daily_usd is None
        assert loaded2.budget.timezone is None
