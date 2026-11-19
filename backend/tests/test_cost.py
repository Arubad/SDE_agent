"""Per-span cost from the Haiku 5.5 price table (USD per million tokens)."""
import pytest

from backend.core import config
from backend.core.cost import span_cost

M = 1_000_000


@pytest.mark.parametrize("tokens, expected", [
    ({"input_tokens": M}, 0.10),
    ({"output_tokens": M}, 0.50),
    ({"cache_read_tokens": M}, 0.01),
    ({"cache_write_1h_tokens": M}, 0.20),
    ({"cache_write_5m_tokens": M}, 0.125),
    ({"cache_write_tokens": M}, 0.20),  # no 5m/1h split: priced at the 1-hour rate
    ({"cache_write_tokens": M, "cache_write_5m_tokens": M}, 0.125),
])
def test_haiku_prices(tokens, expected):
    assert span_cost({"model": "claude-haiku-5-5", **tokens}) == pytest.approx(expected)


def test_unknown_model_costs_nothing():
    assert span_cost({"model": "gemini-3.5-flash", "input_tokens": M}) == 0.0
    assert span_cost({"input_tokens": M}) == 0.0


def test_price_table_override(monkeypatch):
    monkeypatch.setattr(config, "PRICE_TABLE_JSON", '{"m":{"input":1}}')
    assert span_cost({"model": "m", "input_tokens": M}) == pytest.approx(1.0)
    assert span_cost({"model": "claude-haiku-5-5", "input_tokens": M}) == pytest.approx(0.10)
