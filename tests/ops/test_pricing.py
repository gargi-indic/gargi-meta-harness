import pytest

from harness.config import GlobalConfig, load_global, save_global
from harness.ops.pricing import ModelPrice, PricingTable, default_table, estimate_cost


def test_known_model_is_priced_from_the_table_and_not_estimated():
    cost = estimate_cost("claude-sonnet", 2_000_000, 1_000_000)
    price = default_table().models["claude-sonnet"]
    assert cost.estimated is False
    assert cost.usd == pytest.approx(2 * price.input_per_mtok + price.output_per_mtok)


def test_a_dated_model_name_matches_its_longest_prefix():
    assert estimate_cost("claude-sonnet-4-20250101", 1_000_000, 0).estimated is False


def test_unknown_model_uses_the_fallback_and_is_estimated():
    cost = estimate_cost("mystery-model", 1_000_000, 1_000_000)
    unknown = default_table().unknown
    assert cost.estimated is True
    assert cost.usd == pytest.approx(unknown.input_per_mtok + unknown.output_per_mtok)
    assert estimate_cost(None, 10, 10).estimated is True


def test_missing_token_counts_are_zero():
    assert estimate_cost("claude-sonnet", None, None).usd == 0.0


def test_override_wins_and_can_replace_the_unknown_fallback():
    table = default_table().with_overrides(
        {
            "claude-sonnet": ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0),
            "unknown": ModelPrice(input_per_mtok=10.0, output_per_mtok=10.0),
        }
    )
    assert estimate_cost("claude-sonnet", 1_000_000, 1_000_000, table).usd == pytest.approx(3.0)
    assert estimate_cost("mystery", 1_000_000, 0, table).usd == pytest.approx(10.0)
    assert default_table().with_overrides({}) is default_table()  # nothing to override: the same table


def test_global_config_pricing_round_trips_through_the_toml_file(tmp_path):
    cfg = GlobalConfig(
        data_dir=tmp_path,
        env_files=[],
        pricing={"my-model": ModelPrice(input_per_mtok=1.5, output_per_mtok=7)},
    )
    path = save_global(cfg, tmp_path / "config.toml")
    loaded = load_global(path)
    assert loaded.pricing == cfg.pricing
    table = PricingTable.packaged().with_overrides(loaded.pricing)
    assert estimate_cost("my-model", 1_000_000, 0, table) == estimate_cost("my-model", 1_000_000, 0, table)
    assert estimate_cost("my-model", 1_000_000, 0, table).usd == pytest.approx(1.5)
    assert estimate_cost("my-model", 1_000_000, 0, table).estimated is False


def test_global_config_without_pricing_is_unchanged(tmp_path):
    path = save_global(GlobalConfig(data_dir=tmp_path, env_files=[]), tmp_path / "config.toml")
    assert "pricing" not in path.read_text()
    assert load_global(path).pricing == {}
