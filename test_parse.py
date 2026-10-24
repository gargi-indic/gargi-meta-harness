from harness.config import GlobalConfig, load_global, save_global
import json
import tomllib
from pathlib import Path
cfg = GlobalConfig()
cfg.budget.daily_usd = 42.5
cfg.budget.timezone = "America/New_York"
save_global(cfg, Path("test.toml"))
with open("test.toml", "rb") as f:
    data = tomllib.load(f)
    print("data:", data)
    print("parsed:", GlobalConfig(**data).budget)
