import pytest

from backend.core.redact import redact, redact_obj


def test_redacts_pii_and_keys(monkeypatch):
    monkeypatch.setenv("PREMIUM_PASSCODE", "hunter2-passcode")
    cases = {
        "mail me at jane.doe@example.com now": ("[email]", "jane.doe@example.com"),
        "call +1 (415) 555-0199 today": ("[phone]", "555-0199"),
        "key sk-ant-api03-abcdefghijklmnop": ("[secret]", "sk-ant-api03"),
        "token hf_abcdefghijklmnopqrstu": ("[secret]", "hf_abcdefghijklmnopqrstu"),
        "Authorization: Bearer abc.def.ghi": ("Bearer [secret]", "abc.def.ghi"),
        "code is hunter2-passcode ok": ("[secret]", "hunter2-passcode"),
    }
    for text, (placeholder, original) in cases.items():
        out = redact(text)
        assert placeholder in out, text
        assert original not in out, text


@pytest.mark.parametrize("text", [
    "task-based scheduling",
    "watch 12:30–18:00",
    "chunk 9f2c1a7b3e",
    "https://example.com/docs?page=2",
    "see [3]",
    "in 2026",
    "used 1884 tokens on v1.2.3",
    "2026-11-19 10:30",
    "2026-11-19",
    "tokens 1884 2000 3000 4000",
    "ids 12345 67890",
    "ts 1731974400123",
    "v1.2.3",
    "1884",
])
def test_redact_leaves_ordinary_text(text):
    assert redact(text) == text


@pytest.mark.parametrize("text", [
    "+1 (415) 555-0199", "415-555-0199", "415.555.0199", "+14155550199",
    "4155550199", "+44 20 7946 0958", "(415) 555-0199",
])
def test_redact_phone_shapes(text):
    assert redact("call " + text + " now") == "call [phone] now"


def test_longer_secret_replaced_first(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "abcdef")
    monkeypatch.setenv("GITHUB_TOKEN", "abcdef-longer-value")
    assert redact("x abcdef-longer-value y") == "x [secret] y"


def test_short_secret_ignored(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "abc")
    assert redact("abc def") == "abc def"


def test_redact_obj_nested():
    src = {"a": ["x@y.com", 3, {"b": "Bearer t"}], "t": ("x@y.com",)}
    out = redact_obj(src)
    assert out == {"a": ["[email]", 3, {"b": "Bearer [secret]"}], "t": ("[email]",)}
    assert src["a"][0] == "x@y.com"


def test_trace_log_is_redacted(capsys):
    from backend.core.trace import Trace
    t = Trace("a@b.com")
    t.log()
    out = capsys.readouterr().out
    assert "[email]" in out and "a@b.com" not in out


def test_foundry_api_key_alternate_name_redacted(monkeypatch):
    from backend.core.redact import SECRET_ENV_NAMES

    assert "ANTHROPIC_FOUNDRY_API_KEY" in SECRET_ENV_NAMES
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "foundry-key-value-123")
    out = redact("using foundry-key-value-123 now")
    assert "foundry-key-value-123" not in out and "[secret]" in out
