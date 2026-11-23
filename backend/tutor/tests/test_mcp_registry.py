import json
from pathlib import Path

import pytest

from backend.tutor.mcp.registry import (
    Registry,
    RegistryError,
    ServerSpec,
    effective_enabled,
    load_registry,
    missing_env,
    set_enabled,
)

COMMITTED = Path(__file__).resolve().parents[1] / "mcp" / "servers.json"


def _write(tmp_path, servers, agents=None):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": servers, "agents": agents or {}}))
    return p


def _inproc(name, ns=None, **kw):
    return {"name": name, "namespace": ns or name, "transport": "inproc", "target": name, **kw}


def test_committed_registry_loads():
    reg = load_registry(COMMITTED, {}, allow_unknown=True)
    assert isinstance(reg, Registry)
    assert {s.namespace for s in reg.servers} >= {"kb", "learner", "ingest", "web"}
    assert all(sp.untrusted for sp in reg.servers)
    assert reg.agents["crawler"] == ("browser.navigate", "browser.evaluate", "browser.close")


def test_env_expansion_and_default(tmp_path):
    p = _write(tmp_path, [{
        "name": "browser", "namespace": "browser", "transport": "http",
        "url": "${PLAYWRIGHT_MCP_URL:-http://127.0.0.1:8931/mcp}",
        "headers": {"Authorization": "Bearer ${TOK}"},
    }])
    default = load_registry(p, {}).servers[0]
    assert default.url == "http://127.0.0.1:8931/mcp"
    assert default.headers == {"Authorization": "Bearer "}
    set_ = load_registry(p, {"PLAYWRIGHT_MCP_URL": "http://h:1/mcp", "TOK": "x"}).servers[0]
    assert set_.url == "http://h:1/mcp"
    assert set_.headers == {"Authorization": "Bearer x"}


def test_unpinned_uvx_rejected(tmp_path):
    def entry(pkg):
        return {"name": "papers", "namespace": "papers", "transport": "stdio",
                "command": "uvx", "args": ["--quiet", pkg]}
    with pytest.raises(RegistryError, match="pinned"):
        load_registry(_write(tmp_path, [entry("arxiv-mcp")]), {})
    assert load_registry(_write(tmp_path, [entry("arxiv-mcp==1.2.3")]), {}).servers[0].args[1] == "arxiv-mcp==1.2.3"


def test_duplicate_namespace_rejected(tmp_path):
    with pytest.raises(RegistryError, match="duplicate"):
        load_registry(_write(tmp_path, [_inproc("a", "x"), _inproc("b", "x")]), {})
    with pytest.raises(RegistryError, match="duplicate"):
        load_registry(_write(tmp_path, [_inproc("a", "x"), _inproc("a", "y")]), {})


def test_unknown_transport_and_keys_rejected(tmp_path):
    with pytest.raises(RegistryError, match="transport"):
        load_registry(_write(tmp_path, [{"name": "a", "namespace": "a", "transport": "carrier-pigeon"}]), {})
    with pytest.raises(RegistryError, match="unknown"):
        load_registry(_write(tmp_path, [_inproc("a", bogus=1)]), {})


def test_agent_pattern_matching_no_namespace(tmp_path):
    p = _write(tmp_path, [_inproc("kb")], {"teacher": ["kb.*", "nope.*"]})
    with pytest.raises(RegistryError, match="nope"):
        load_registry(p, {})
    assert load_registry(p, {}, allow_unknown=True).agents["teacher"] == ("kb.*", "nope.*")


def test_patterns_for_dotted_namespaces_validate(tmp_path):
    servers = [{"name": "deepwiki", "namespace": "repo.deepwiki", "transport": "http", "url": "https://x.invalid/mcp"},
               {"name": "context7", "namespace": "docs.context7", "transport": "http", "url": "https://y.invalid/mcp"}]
    for pat in ("repo.*", "docs.*", "repo.deepwiki.*", "repo.deepwiki.read_wiki", "*", "repo.deep*", "*.read_wiki"):
        reg = load_registry(_write(tmp_path, servers, {"teacher": [pat]}), {})
        assert reg.agents["teacher"] == (pat,)
        p = tmp_path / "servers.json"
        p.write_text(json.dumps({"servers": servers, "agents": {}, "rules": {"ask": [pat]}}))
        assert load_registry(p, {}).ask == (pat,)
    for pat in ("bogus.*", "repo.other.*", "docs.context8.x"):
        with pytest.raises(RegistryError, match="matches no server namespace"):
            load_registry(_write(tmp_path, servers, {"teacher": [pat]}), {})


def test_missing_env_reported(tmp_path):
    p = _write(tmp_path, [{"name": "github", "namespace": "repo", "transport": "http",
                           "url": "https://example.invalid/mcp", "requires_env": ["GITHUB_TOKEN"]}])
    spec = load_registry(p, {}).servers[0]
    assert missing_env(spec, {}) == ["GITHUB_TOKEN"]
    assert missing_env(spec, {"GITHUB_TOKEN": ""}) == ["GITHUB_TOKEN"]
    assert missing_env(spec, {"GITHUB_TOKEN": "t"}) == []


def test_spec_repr_hides_headers():
    spec = ServerSpec(name="g", namespace="g", transport="http", url="u", headers={"Authorization": "ghp_secret"})
    assert "ghp_secret" not in repr(spec)


def test_effective_enabled_without_engine():
    on = ServerSpec(name="a", namespace="a", transport="inproc", target="a", enabled_default=True)
    off = ServerSpec(name="b", namespace="b", transport="inproc", target="b", enabled_default=False)
    assert effective_enabled(None, on) is True
    assert effective_enabled(None, off) is False


def test_enabled_override_roundtrip(engine):
    spec = ServerSpec(name="rt_srv", namespace="rt", transport="inproc", target="x", enabled_default=True)
    assert effective_enabled(engine, spec) is True
    set_enabled(engine, "rt_srv", False)
    assert effective_enabled(engine, spec) is False
    set_enabled(engine, "rt_srv", True)
    assert effective_enabled(engine, spec) is True
