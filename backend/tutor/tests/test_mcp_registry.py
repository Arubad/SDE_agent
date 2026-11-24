import json
import re
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
    reg = load_registry(COMMITTED, {})  # strict: every agent / rules pattern names a namespace
    assert isinstance(reg, Registry)
    assert {s.namespace for s in reg.servers} >= {"kb", "learner", "ingest", "web"}
    assert all(sp.untrusted for sp in reg.servers)
    assert reg.agents["crawler"] == ("browser.navigate", "browser.evaluate", "browser.close")


def test_committed_external_servers():
    reg = load_registry(COMMITTED, {})
    by = {s.name: s for s in reg.servers}
    assert {n: (by[n].namespace, by[n].transport) for n in
            ("playwright", "deepwiki", "mslearn", "arxiv", "wikipedia", "context7", "github", "fetch")} == {
        "playwright": ("browser", "http"), "deepwiki": ("repo.deepwiki", "http"),
        "mslearn": ("docs.mslearn", "http"), "arxiv": ("papers.arxiv", "stdio"),
        "wikipedia": ("ref.wikipedia", "stdio"), "context7": ("docs.context7", "http"),
        "github": ("repo.github", "http"), "fetch": ("fetch", "stdio"),
    }
    pw = by["playwright"]
    assert (pw.url, pw.strip_prefix, pw.timeout_s) == ("http://127.0.0.1:8931/mcp", "browser_", 45.0)
    assert load_registry(COMMITTED, {"PLAYWRIGHT_MCP_URL": "http://playwright-mcp:8931/mcp"}).servers[
        [s.name for s in reg.servers].index("playwright")].url == "http://playwright-mcp:8931/mcp"
    assert by["deepwiki"].url == "https://mcp.deepwiki.com/mcp"
    assert by["mslearn"].url == "https://learn.microsoft.com/api/mcp"
    assert by["context7"].url == "https://mcp.context7.com/mcp"
    assert by["context7"].requires_env == ("CONTEXT7_API_KEY",)
    assert by["github"].url == "https://api.githubcopilot.com/mcp/"
    assert by["github"].requires_env == ("GITHUB_TOKEN",)
    env = {"GITHUB_TOKEN": "ghp_x", "CONTEXT7_API_KEY": "c7_y"}
    by_env = {s.name: s for s in load_registry(COMMITTED, env).servers}
    assert by_env["github"].headers == {"Authorization": "Bearer ghp_x", "X-MCP-Readonly": "true"}
    assert by_env["context7"].headers == {"CONTEXT7_API_KEY": "c7_y"}
    stdio = [s for s in reg.servers if s.transport == "stdio"]
    assert {s.name for s in stdio} == {"arxiv", "wikipedia", "fetch"}
    for s in stdio:
        assert s.command == "uvx"
        pkg = next(a for a in s.args if not a.startswith("-"))
        assert re.fullmatch(r"[a-z0-9-]+==\d+(\.\d+)+", pkg), pkg
    assert by["arxiv"].args[-2:] == ("--storage-path", "/tmp/roadmap-tutor/arxiv")
    with_path = {s.name: s for s in load_registry(COMMITTED, {"ARXIV_STORAGE_PATH": "/data/arxiv"}).servers}
    assert with_path["arxiv"].args[-1] == "/data/arxiv"
    assert [s.name for s in reg.servers if not s.enabled_default] == ["fetch"]
    assert "private-address" in by["fetch"].note
    for s in reg.servers:  # every external server is untrusted (its results are fenced)
        if s.transport != "inproc":
            assert s.untrusted


def test_env_expansion_and_default(tmp_path):
    p = _write(tmp_path, [{
        "name": "browser", "namespace": "browser", "transport": "http",
        "url": "${PLAYWRIGHT_MCP_URL:-http://127.0.0.1:8931/mcp}",
        "headers": {"Authorization": "Bearer ${TOK}"}, "requires_env": ["TOK"],
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


def test_secret_vars_must_be_required(tmp_path):
    def entry(**kw):
        return {"name": "s", "namespace": "s", "transport": "http", "url": "https://x.invalid/mcp", **kw}
    for bad in (entry(headers={"Authorization": "Bearer ${TOK}"}),
                entry(url="https://x.invalid/mcp?key=${TOK}"),
                {"name": "s", "namespace": "s", "transport": "stdio", "command": "uvx",
                 "args": ["pkg==1.0", "--token", "${TOK}"]},
                entry(headers={"A": "${OTHER}"}, requires_env=["TOK"])):
        with pytest.raises(RegistryError, match="requires_env"):
            load_registry(_write(tmp_path, [bad]), {"TOK": "t", "OTHER": "o"})
    ok = [entry(headers={"Authorization": "Bearer ${TOK}"}, requires_env=["TOK"]),
          entry(url="${URL:-https://x.invalid/mcp}"),  # a default means it is configuration, not a secret
          {"name": "s", "namespace": "s", "transport": "stdio", "command": "uvx",
           "args": ["pkg==1.0", "--dir", "${DIR:-/tmp/x}"]}]
    for good in ok:
        assert load_registry(_write(tmp_path, [good]), {}).servers[0].name == "s"


@pytest.mark.parametrize("field,value", [
    ("enabled", "false"), ("enabled", 0), ("enabled", None),
    ("untrusted", "no"), ("untrusted", 1),
    ("timeout_s", "5"), ("timeout_s", 0), ("timeout_s", -1), ("timeout_s", True), ("timeout_s", None),
    ("args", "pkg==1"), ("args", [1]), ("args", {"a": "b"}),
    ("requires_env", "TOK"), ("requires_env", [3]),
    ("headers", ["A: b"]), ("headers", {"A": 1}), ("headers", "A: b"),
])
def test_field_types_strict(tmp_path, field, value):
    raw = _inproc("a", **{field: value})
    with pytest.raises(RegistryError, match=field):
        load_registry(_write(tmp_path, [raw]), {})


def test_field_types_accepted(tmp_path):
    raw = _inproc("a", enabled=False, untrusted=True, timeout_s=2, args=["x"], requires_env=["E"],
                  headers={"H": "v"})
    spec = load_registry(_write(tmp_path, [raw]), {}).servers[0]
    assert (spec.enabled_default, spec.untrusted, spec.timeout_s, spec.args) == (False, True, 2.0, ("x",))
    assert spec.headers == {"H": "v"} and spec.requires_env == ("E",)
    assert load_registry(_write(tmp_path, [_inproc("a", timeout_s=0.5)]), {}).servers[0].timeout_s == 0.5
