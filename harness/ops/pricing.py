"""Token prices: ``estimate_cost(model, input_tokens, output_tokens)`` gives a :class:`Cost`.

The packaged ``pricing.toml`` holds placeholder values (see its header), not real prices. A ``[pricing]``
table in the global config overrides it per model. A model that is in neither is priced with the explicit
``unknown`` fallback and the result carries ``estimated=True``.
"""

import tomllib
from collections.abc import Mapping
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PRICING_FILE = Path(__file__).with_name("pricing.toml")
_PER_MILLION = 1_000_000


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_per_mtok: float = Field(ge=0, description="USD per million input tokens")
    output_per_mtok: float = Field(ge=0, description="USD per million output tokens")


class Cost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usd: float
    # True when the model has no entry of its own and the unknown fallback was used.
    estimated: bool


class PricingTable:
    def __init__(self, models: Mapping[str, ModelPrice], unknown: ModelPrice) -> None:
        self.models = dict(models)
        self.unknown = unknown

    @classmethod
    def packaged(cls) -> "PricingTable":
        data = tomllib.loads(PRICING_FILE.read_text(encoding="utf-8"))
        return cls(
            {name: ModelPrice(**price) for name, price in data.get("models", {}).items()},
            ModelPrice(**data["unknown"]),
        )

    def with_overrides(self, overrides: Mapping[str, ModelPrice] | None) -> "PricingTable":
        """A copy where ``overrides`` win; the key ``unknown`` replaces the fallback price."""
        if not overrides:
            return self
        models = dict(self.models)
        unknown = self.unknown
        for name, price in overrides.items():
            if name == "unknown":
                unknown = price
            else:
                models[name] = price
        return PricingTable(models, unknown)

    def lookup(self, model: str | None) -> ModelPrice | None:
        """The price of ``model`` (exact name, else the longest prefix key), or None if it is unknown."""
        if not model:
            return None
        if model in self.models:
            return self.models[model]
        prefixes = [name for name in self.models if model.startswith(name)]
        return self.models[max(prefixes, key=len)] if prefixes else None

    def estimate_cost(self, model: str | None, input_tokens: int | None, output_tokens: int | None) -> Cost:
        price = self.lookup(model)
        used = price or self.unknown
        usd = (
            (input_tokens or 0) * used.input_per_mtok + (output_tokens or 0) * used.output_per_mtok
        ) / _PER_MILLION
        return Cost(usd=usd, estimated=price is None)


@cache
def default_table() -> PricingTable:
    return PricingTable.packaged()


def estimate_cost(
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    table: PricingTable | None = None,
) -> Cost:
    return (table or default_table()).estimate_cost(model, input_tokens, output_tokens)
