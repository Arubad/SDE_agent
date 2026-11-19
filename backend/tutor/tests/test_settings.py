from pathlib import Path

import pytest

from backend.core import config
from backend.tutor.settings import SettingsError, load_settings


def test_defaults():
    s = load_settings({})
    assert s.database_url == "postgresql+psycopg://tutor:tutor@127.0.0.1:5432/tutor"
    assert s.qdrant_url == "http://127.0.0.1:6333"
    assert s.collection == "roadmap_chunks"
    assert s.public_mode is False
    assert s.otel_endpoint is None
    assert s.web_dist == Path(__file__).resolve().parents[3] / "web" / "dist"
    assert s.host == "127.0.0.1"
    assert s.port == 8000


def test_public_mode_requires_passcode(monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "")
    with pytest.raises(SettingsError):
        load_settings({"PUBLIC_MODE": "true"}).validate()
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "secret")
    load_settings({"PUBLIC_MODE": "true"}).validate()
    monkeypatch.setattr(config, "PREMIUM_PASSCODE", "")
    load_settings({}).validate()


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "YES", "on", " On "])
def test_public_mode_true_spellings(value):
    assert load_settings({"PUBLIC_MODE": value}).public_mode is True


@pytest.mark.parametrize("value", ["", "  ", "false", "FALSE", "0", "no", "No", "off", "OFF"])
def test_public_mode_false_spellings(value):
    assert load_settings({"PUBLIC_MODE": value}).public_mode is False


@pytest.mark.parametrize("value", ["ture", "enabled", "2", "y", "public", "true!", "t"])
def test_public_mode_rejects_unknown_values(value):
    with pytest.raises(SettingsError, match="PUBLIC_MODE"):
        load_settings({"PUBLIC_MODE": value})
