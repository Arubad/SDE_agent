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


def test_repr_hides_secrets():
    assert "tutor:tutor" not in repr(load_settings({}))
    assert "sekret-key" not in repr(load_settings({"YOUTUBE_API_KEY": "sekret-key"}))


def test_transcript_routes_parse():
    assert load_settings({"TRANSCRIPT_ROUTES": "gemini,captions"}).transcript_routes == ("gemini", "captions")
    assert load_settings({}).transcript_routes == ("captions",)
    with pytest.raises(SettingsError):
        load_settings({"TRANSCRIPT_ROUTES": "whisper"})
    with pytest.raises(SettingsError):
        load_settings({"TRANSCRIPT_ROUTES": "ytdlp_cookies"})
    s = load_settings({"TRANSCRIPT_ROUTES": "ytdlp_cookies", "YTDLP_COOKIES_BROWSER": "firefox"})
    assert s.transcript_routes == ("ytdlp_cookies",) and s.ytdlp_cookies_browser == "firefox"


def test_bad_numbers_raise_settings_error():
    with pytest.raises(SettingsError):
        load_settings({"PORT": "abc"})
    with pytest.raises(SettingsError):
        load_settings({"INGEST_WORKERS": "-1"})
    assert load_settings({"INGEST_WORKERS": "0"}).ingest_workers == 0


def test_ingestion_defaults_and_embedder():
    s = load_settings({})
    assert (s.searxng_url, s.youtube_api_key, s.ytdlp_cookies_browser, s.net_fixtures_dir) == (
        "http://127.0.0.1:8888", None, None, None)
    assert (s.ingest_workers, s.crawl_max_depth, s.crawl_max_pages) == (2, 2, 50)
    assert (s.domain_interval_s, s.playlist_max_videos, s.embedder) == (1.0, 50, "bge")
    assert load_settings({"EMBEDDER": "hash"}).embedder == "hash"
    assert load_settings({"NET_FIXTURES_DIR": "/x"}).net_fixtures_dir == Path("/x")
    assert load_settings({"PLATFORM_QDRANT_URL": ":memory:"}).qdrant_url == ":memory:"
    with pytest.raises(SettingsError):
        load_settings({"EMBEDDER": "openai"})


def test_empty_searxng_url_disables_search_but_unset_keeps_default():
    assert load_settings({"SEARXNG_URL": ""}).searxng_url is None
    assert load_settings({"SEARXNG_URL": "  "}).searxng_url is None
    assert load_settings({}).searxng_url == "http://127.0.0.1:8888"
    assert load_settings({"SEARXNG_URL": "http://s:1"}).searxng_url == "http://s:1"


def test_domain_interval_accepts_zero_but_not_non_finite_or_negative():
    assert load_settings({"DOMAIN_INTERVAL_S": "0"}).domain_interval_s == 0.0
    assert load_settings({"DOMAIN_INTERVAL_S": "2.5"}).domain_interval_s == 2.5
    for bad in ("inf", "nan", "-1", "abc"):
        with pytest.raises(SettingsError, match="DOMAIN_INTERVAL_S"):
            load_settings({"DOMAIN_INTERVAL_S": bad})


def test_blank_cookies_browser_counts_as_unset():
    assert load_settings({"YTDLP_COOKIES_BROWSER": "   "}).ytdlp_cookies_browser is None
    with pytest.raises(SettingsError):
        load_settings({"TRANSCRIPT_ROUTES": "ytdlp_cookies", "YTDLP_COOKIES_BROWSER": "  "})


def test_mcp_registry_setting():
    default = Path(__file__).resolve().parents[1] / "mcp" / "servers.json"
    assert load_settings({}).mcp_registry == default
    assert load_settings({"MCP_REGISTRY": "/x/s.json"}).mcp_registry == Path("/x/s.json")


def test_browser_render_parse():
    assert load_settings({}).browser_render is True
    assert load_settings({"BROWSER_RENDER": " "}).browser_render is True
    assert load_settings({"BROWSER_RENDER": "off"}).browser_render is False
    with pytest.raises(SettingsError, match="BROWSER_RENDER"):
        load_settings({"BROWSER_RENDER": "maybe"})


def test_tutor_defaults():
    s = load_settings({})
    assert (s.tutor_max_steps, s.tutor_max_tokens, s.tutor_max_usd) == (6, 60_000, 0.02)
    assert (s.working_memory_tokens, s.ingest_summaries, s.ingest_summary_budget_usd) == (12_000, True, 0.50)


def test_tutor_overrides():
    s = load_settings({"TUTOR_MAX_STEPS": "3", "TUTOR_MAX_TOKENS": "1000", "TUTOR_MAX_USD": "0.005",
                       "WORKING_MEMORY_TOKENS": "500", "INGEST_SUMMARIES": "off",
                       "INGEST_SUMMARY_BUDGET_USD": "1.25"})
    assert (s.tutor_max_steps, s.tutor_max_tokens, s.tutor_max_usd) == (3, 1000, 0.005)
    assert (s.working_memory_tokens, s.ingest_summaries, s.ingest_summary_budget_usd) == (500, False, 1.25)


@pytest.mark.parametrize("env", [
    {"TUTOR_MAX_STEPS": "0"}, {"TUTOR_MAX_TOKENS": "x"}, {"TUTOR_MAX_USD": "-1"}, {"TUTOR_MAX_USD": "nan"},
    {"TUTOR_MAX_USD": "abc"}, {"WORKING_MEMORY_TOKENS": "0"}, {"INGEST_SUMMARIES": "maybe"},
    {"INGEST_SUMMARY_BUDGET_USD": "inf"},
])
def test_tutor_settings_rejected(env):
    with pytest.raises(SettingsError):
        load_settings(env)
