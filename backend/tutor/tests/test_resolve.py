from pathlib import Path

import pytest

from backend.tutor.ingest.net import SearchResult, SearchUnavailable, fixture_net
from backend.tutor.roadmap.parse import normalize_spec
from backend.tutor.roadmap.resolve import resolve_spec
from backend.tutor.roadmap.spec import ResourceSpec, RoadmapSpec, StageSpec
import json

FIXTURES = Path(__file__).parent / "fixtures"


def _k8s_spec():
    raw = json.loads((FIXTURES / "llm_replies" / "RoadmapSpec.json").read_text())
    return normalize_spec(raw, (FIXTURES / "roadmaps" / "kubernetes.txt").read_text())


def _spec(*resources: ResourceSpec) -> RoadmapSpec:
    stage = StageSpec(id="s", title="S", timeframe=None, summary="", skills=[], resources=list(resources),
                      practice_goals=[])
    return RoadmapSpec(title="T", goals=[], weekly_hours=None, stages=[stage])


def _res(**kw) -> ResourceSpec:
    base = dict(id="r", title="Intro to Rust", kind="article", url=None, url_hint=None, query="rust intro",
                skill_ids=[])
    return ResourceSpec(**{**base, **kw})


class StubSearch:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results or [], error, []

    def search(self, query, *, category="general"):
        self.calls.append((query, category))
        if self.error:
            raise self.error
        return self.results


def _net(search):
    net = fixture_net(FIXTURES / "net")
    net.search = search
    return net


def test_resolve_kubernetes_roadmap():
    spec = _k8s_spec()
    out = {r.resource_id: r for r in resolve_spec(spec, fixture_net(FIXTURES / "net"))}
    assert len(out) == 8  # stage 4 has no resources and contributes nothing
    videos = [r for st in spec.stages for x in st.resources if x.kind == "video" for r in [out[x.id]]]
    assert len(videos) == 5
    for res in videos:
        assert res.status == "resolved"
        top = res.candidates[0]
        assert "youtube.com" in top.url and top.source == "search" and top.score >= 0.75
    docs = [out["res-k8s-docs-concepts"], out["res-k8s-tutorials"]]
    for res in docs:
        assert res.candidates[0].url.startswith("https://kubernetes.io/")
    killercoda = out["res-killercoda"]
    assert killercoda.status == "linked_only" and killercoda.candidates
    assert killercoda.candidates[0].source == "url_hint"
    assert all(len(r.candidates) <= 3 for r in out.values())
    scores = [c.score for c in out["res-k8s-beginners-course"].candidates]
    assert scores == sorted(scores, reverse=True)


def test_existing_url_skips_network():
    net = _net(StubSearch(error=AssertionError("search must not be called")))
    out = resolve_spec(_spec(_res(url="https://example.test/pods")), net)
    assert out[0].status == "has_url" and out[0].candidates == []


def test_search_unavailable_means_needs_url():
    for search in (None, StubSearch(error=SearchUnavailable("down"))):
        out = resolve_spec(_spec(_res()), _net(search))
        assert out[0].status == "needs_url"
        assert out[0].message == "Search is unavailable; paste a URL"
        assert out[0].candidates == []


def test_unverifiable_url_hint_is_not_trusted():
    good = SearchResult("Intro to Rust", "https://example.test/rust-intro", "", "fixture")
    net = _net(StubSearch([good]))
    out = resolve_spec(_spec(_res(url_hint="https://example.test/gone")), net)[0]
    by_source = {c.source: c for c in out.candidates}
    assert by_source["url_hint"].score < by_source["search"].score
    assert out.candidates[0].source == "search"


def test_verified_url_hint_is_boosted():
    # pods.html is titled "Pods"
    net = _net(StubSearch())
    out = resolve_spec(_spec(_res(title="Pods", url_hint="https://example.test/pods")), net)[0]
    assert out.candidates[0].source == "url_hint" and out.candidates[0].score >= 0.75


def test_low_confidence_needs_url_keeps_candidates():
    weak = SearchResult("Cooking with cast iron", "https://example.test/cooking", "", "fixture")
    out = resolve_spec(_spec(_res()), _net(StubSearch([weak])))[0]
    assert out.status == "needs_url"
    assert [c.url for c in out.candidates] == ["https://example.test/cooking"]
    assert out.candidates[0].score < 0.75


def test_candidates_capped_at_three_and_category():
    results = [SearchResult("Intro to Rust", f"https://example.test/{i}", "", "x") for i in range(6)]
    search = StubSearch(results)
    out = resolve_spec(_spec(_res(), _res(id="v", kind="playlist")), _net(search))
    assert all(len(r.candidates) <= 3 for r in out)
    assert [c for _, c in search.calls] == ["general", "videos"]
