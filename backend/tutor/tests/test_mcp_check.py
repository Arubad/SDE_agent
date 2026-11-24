"""`python -m backend.tutor mcp-check [--json]`: a health report of every registry server, never a gate.
No network: the servers here are in-process, unconfigured, disabled, or a refused loopback port."""
import json
from pathlib import Path

import pytest

from backend.tutor.mcp.registry import set_enabled

FIXTURES = Path(__file__).parent / "fixtures"
DOWN_URL = "postgresql+psycopg://x:y@127.0.0.1:9/z"
SECRET = "sekrit-token-0123456789"


def _registry(tmp_path):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": [
        {"name": "chk_web", "namespace": "chk_web", "transport": "inproc", "target": "web"},
        {"name": "chk_secret", "namespace": "chk_secret", "transport": "http", "url": "http://127.0.0.1:9/mcp",
         "headers": {"Authorization": "Bearer ${CHK_TOKEN}"}, "requires_env": ["CHK_TOKEN"], "timeout_s": 5},
        {"name": "chk_needs", "namespace": "chk_needs", "transport": "http", "url": "https://x.invalid/mcp",
         "requires_env": ["CHK_UNSET_TOKEN"]},
        {"name": "chk_off", "namespace": "chk_off", "transport": "stdio", "command": "uvx", "args": ["pkg==1.0"],
         "enabled": False},
    ], "agents": {}}))
    return p


@pytest.fixture
def check_env(tmp_path, monkeypatch):
    for k, v in {"DATABASE_URL": DOWN_URL, "NET_FIXTURES_DIR": str(FIXTURES / "net"), "EMBEDDER": "hash",
                 "PLATFORM_QDRANT_URL": ":memory:", "MCP_REGISTRY": str(_registry(tmp_path)),
                 "CHK_TOKEN": SECRET}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CHK_UNSET_TOKEN", raising=False)


def _rows(out: str) -> dict[str, list[str]]:
    return {line.split()[0]: line.split(None, 4) for line in out.strip().splitlines()[1:]}


def test_mcp_check_lines(check_env, capsys):
    from backend.tutor.__main__ import main

    main(["mcp-check"])  # returns (exit 0) although servers are down
    cap = capsys.readouterr()
    assert cap.out.splitlines()[0].split() == ["name", "state", "tools", "latency_ms", "error"]
    rows = _rows(cap.out)
    assert rows["chk_web"][:3] == ["chk_web", "ok", "2"]
    assert rows["chk_secret"][1] == "down" and rows["chk_secret"][2] == "0"
    assert rows["chk_needs"][1:3] == ["missing_env", "0"]
    assert rows["chk_needs"][4] == "missing env: CHK_UNSET_TOKEN"
    assert rows["chk_off"][1:5] == ["disabled", "0", "-", "-"]
    assert SECRET not in cap.out + cap.err
    assert "database unavailable" in cap.err  # the overrides could not be read: registry defaults


def test_mcp_check_json(check_env, capsys):
    from backend.tutor.__main__ import main

    main(["mcp-check", "--json"])
    out = capsys.readouterr().out
    data = {h["name"]: h for h in json.loads(out)}
    assert set(data) == {"chk_web", "chk_secret", "chk_needs", "chk_off"}
    assert data["chk_web"]["state"] == "ok" and data["chk_web"]["tools"] == 2
    assert isinstance(data["chk_web"]["latency_ms"], int)
    assert data["chk_secret"]["state"] == "down" and data["chk_secret"]["error"]
    assert data["chk_off"]["state"] == "disabled"
    assert SECRET not in out


def test_mcp_check_bad_registry_reports(check_env, tmp_path, monkeypatch, capsys):
    from backend.tutor.__main__ import main

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"servers": [{"name": "a", "namespace": "a", "transport": "inproc",
                                            "enabled": "false"}]}))
    monkeypatch.setenv("MCP_REGISTRY", str(bad))
    main(["mcp-check"])
    assert "enabled must be true or false" in capsys.readouterr().err


def test_mcp_check_reads_overrides(check_env, db_url, engine, monkeypatch, capsys):
    from backend.tutor.__main__ import main

    monkeypatch.setenv("DATABASE_URL", db_url)
    set_enabled(engine, "chk_web", False)
    try:
        main(["mcp-check", "--json"])
        cap = capsys.readouterr()
        assert {h["name"]: h["state"] for h in json.loads(cap.out)}["chk_web"] == "disabled"
        assert "database unavailable" not in cap.err
    finally:
        set_enabled(engine, "chk_web", True)
