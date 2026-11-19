"""LLM cost from span token counts. Prices are USD per million tokens, overridable with PRICE_TABLE_JSON."""
from __future__ import annotations

import json

from backend.core import config

_BASE_PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01,
                         "cache_write_5m": 0.125, "cache_write_1h": 0.20},
}


def _merged(overrides_json: str) -> dict[str, dict[str, float]]:
    table = {model: dict(rates) for model, rates in _BASE_PRICES.items()}
    for model, rates in (json.loads(overrides_json) if overrides_json else {}).items():
        table.setdefault(model, {}).update(rates)
    return table


PRICES: dict[str, dict[str, float]] = _merged(config.PRICE_TABLE_JSON)


def _prices() -> dict[str, dict[str, float]]:
    # Re-read the override so a changed config.PRICE_TABLE_JSON (tests, scripts) takes effect
    return _merged(config.PRICE_TABLE_JSON) if config.PRICE_TABLE_JSON else PRICES


def span_cost(attrs: dict) -> float:
    """USD for one LLM span. Unknown models cost 0.0; a cache write without a 5m/1h split uses the 1-hour rate."""
    rates = _prices().get(attrs.get("model") or "")
    if not rates:
        return 0.0
    write_5m = attrs.get("cache_write_5m_tokens", 0)
    write_1h = attrs.get("cache_write_1h_tokens", 0)
    write_unsplit = max(attrs.get("cache_write_tokens", 0) - write_5m - write_1h, 0)
    usd = (attrs.get("input_tokens", 0) * rates.get("input", 0.0)
           + attrs.get("output_tokens", 0) * rates.get("output", 0.0)
           + attrs.get("cache_read_tokens", 0) * rates.get("cache_read", 0.0)
           + write_5m * rates.get("cache_write_5m", 0.0)
           + (write_1h + write_unsplit) * rates.get("cache_write_1h", 0.0))
    return usd / 1_000_000
