import json
from pathlib import Path

import pytest

from backend.tutor.roadmap.parse import extract_urls, normalize_spec, parse_roadmap
from backend.tutor.roadmap.spec import RoadmapSpec

FIXTURES = Path(__file__).parent / "fixtures"


def _all_resources(spec):
    return [r for st in spec.stages for r in st.resources]


def test_parse_kubernetes_roadmap(monkeypatch):
    monkeypatch.setenv("FAKE_LLM_REPLIES_DIR", str(FIXTURES / "llm_replies"))
    spec = parse_roadmap((FIXTURES / "roadmaps" / "kubernetes.txt").read_text())
    assert len(spec.stages) == 4
    assert spec.stages[3].resources == [] and spec.stages[3].practice_goals
    resources = _all_resources(spec)
    assert len(resources) == 8
    assert all(r.url is None for r in resources)
    killercoda = next(r for r in resources if "Killercoda" in r.title)
    assert killercoda.url_hint == "https://killercoda.com/playgrounds"
    assert killercoda.kind == "interactive"
    ids = [r.id for r in resources] + [s.id for st in spec.stages for s in st.skills] + [st.id for st in spec.stages]
    assert len(ids) == len(set(ids))


def _raw_linked(urls):
    return {
        "title": "Rust", "goals": [], "weekly_hours": None,
        "stages": [{"id": "s", "title": "Week 1", "timeframe": None, "summary": "", "skills": [],
                    "practice_goals": [],
                    "resources": [{"id": f"r{i}", "title": f"R{i}", "kind": "other", "url": u, "url_hint": None,
                                   "query": "q", "note": "", "focus": [], "skill_ids": []}
                                  for i, u in enumerate(urls)]}],
    }


def test_parse_never_invents_or_drops_urls():
    source = (FIXTURES / "roadmaps" / "linked.md").read_text()
    urls = extract_urls(source)
    assert len(urls) == 5
    altered = urls[0] + "?utm=x"
    omitted = urls[4]
    spec = normalize_spec(_raw_linked([altered, *urls[1:4]]), source)
    resources = _all_resources(spec)
    changed = next(r for r in resources if r.url_hint == altered)
    assert changed.url is None
    carried = {r.url for r in resources}
    assert set(urls) <= carried
    assert altered not in carried
    added = next(r for r in resources if r.url == omitted)
    assert added.kind == "other"


def test_extract_urls_trims_punctuation():
    assert extract_urls("see (https://a.dev/x).") == ["https://a.dev/x"]
    assert extract_urls("a https://a.dev/y— b https://a.dev/z, c") == ["https://a.dev/y", "https://a.dev/z"]


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for v in node.values():
            yield from _objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _objects(v)


def test_schema_is_strict():
    schema = RoadmapSpec.json_schema()
    assert schema["title"] == "RoadmapSpec"
    nodes = list(_objects(schema))
    assert nodes
    for node in nodes:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


def test_unknown_prerequisite_dropped():
    raw = _raw_linked([])
    raw["stages"][0]["skills"] = [
        {"id": "a", "title": "A", "objectives": [], "prerequisites": ["b", "ghost"]},
        {"id": "b", "title": "B", "objectives": [], "prerequisites": []},
    ]
    spec = normalize_spec(raw, "")
    a, b = spec.stages[0].skills
    assert a.prerequisites == [b.id]


def test_too_many_resources_raises():
    raw = _raw_linked([])
    raw["stages"] = [dict(raw["stages"][0], id=f"s{i}", resources=[
        {"id": f"r{i}-{j}", "title": "t", "kind": "other", "url": None, "url_hint": None, "query": "q",
         "note": "", "focus": [], "skill_ids": []} for j in range(21)]) for i in range(10)]
    with pytest.raises(ValueError):
        normalize_spec(raw, "")


def test_normalize_is_idempotent():
    raw = json.loads((FIXTURES / "llm_replies" / "RoadmapSpec.json").read_text())
    text = (FIXTURES / "roadmaps" / "kubernetes.txt").read_text()
    once = normalize_spec(raw, text)
    twice = normalize_spec(once.model_dump(mode="json"), text)
    assert twice == once
