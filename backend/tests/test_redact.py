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
])
def test_redact_leaves_ordinary_text(text):
    assert redact(text) == text


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
