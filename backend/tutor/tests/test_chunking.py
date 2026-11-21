import math

from backend.tutor.ingest.chunking import approx_tokens, chunk_markdown
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.ingest.net import Chapter, Cue, VideoMeta
from backend.tutor.ingest.video import video_document

EMB = HashEmbedder()


def _sentences(n, tag="alpha"):
    return " ".join(f"This is sentence number {i} about {tag} topic {i % 7} and more words here." for i in range(n))


def _chunk(md, **kw):
    return chunk_markdown(md, doc_title="Doc", doc_key="doc-1", embedder=EMB, **kw)


def test_approx_tokens():
    assert approx_tokens("") == 0
    assert approx_tokens("a b c d e") == math.ceil(5 * 1.3)


def test_breadcrumbs_and_parent_child():
    md = (f"# Services\n\nIntro text here.\n\n## ClusterIP\n\n{_sentences(40)}\n\n"
          f"## NodePort\n\n{_sentences(40, 'beta')}\n")
    parents, children = _chunk(md)
    paths = [" › ".join(p.path) for p in parents]
    assert "Doc › Services › ClusterIP" in paths
    assert "Doc › Services › NodePort" in paths
    ids = {p.id for p in parents}
    assert children and all(c.parent_id in ids for c in children)
    assert all(c.tokens <= 350 for c in children)
    for c in children:
        assert c.embed_text.endswith("\n" + c.text) and c.embed_text.startswith("Doc › ")
    assert all(p.tokens <= 1500 for p in parents)


def test_code_and_tables_never_split():
    code = "```python\n" + "\n".join(f"x{i} = compute({i})  # step {i}" for i in range(110)) + "\n```"
    table = "| a | b |\n|---|---|\n" + "\n".join(f"| {i} | v{i} |" for i in range(5))
    assert approx_tokens(code) > 350
    md = f"# Code\n\nBefore {_sentences(6)}\n\n{code}\n\nBetween it.\n\n{table}\n\nAfter {_sentences(6)}\n"
    _, children = _chunk(md)
    assert sum(code in c.text for c in children) == 1
    assert sum(table in c.text for c in children) == 1
    big = next(c for c in children if code in c.text)
    assert big.text == code  # an oversized atomic block is its own child, byte-identical


def test_headings_inside_fences_are_not_headings():
    md = "# Top\n\n```bash\n# not a heading\nls\n```\n\nText after.\n"
    parents, _ = _chunk(md)
    assert all("not a heading" not in " ".join(p.path) for p in parents)


def test_long_section_splits_into_parts():
    md = f"# Big\n\n{_sentences(300)}\n"
    parents, children = _chunk(md)
    assert approx_tokens(_sentences(300)) > 3000
    assert len(parents) >= 2
    assert parents[0].path[-1].endswith("(part 1)") and parents[1].path[-1].endswith("(part 2)")
    assert all(p.tokens <= 1500 for p in parents)
    assert {c.parent_id for c in children} == {p.id for p in parents}


def test_small_fragments_merge():
    md = "# A\n\nTiny.\n\n" + _sentences(40) + "\n\nEnd bit.\n"
    _, children = _chunk(md)
    assert all(c.tokens >= 40 for c in children)


def test_video_timestamps_carried():
    meta = VideoMeta("v1", "Kubernetes Tour", "chan", "desc", 4000.0, [])
    chapters = [Chapter(0.0, "Intro"), Chapter(75.0, "Pods"), Chapter(3700.0, "Wrap up")]
    cues = [Cue(5.0, 2.0, _sentences(8)), Cue(80.0, 2.0, _sentences(8, "pods")),
            Cue(3705.0, 2.0, _sentences(8, "wrap"))]
    md = video_document(meta, "captions", cues, "chapters", chapters)
    assert "## [1:01:40] Wrap up" in md
    parents, children = chunk_markdown(md, doc_title=meta.title, doc_key="v1", embedder=EMB)
    by_title = {}
    for c in children:
        pp = next(p for p in parents if p.id == c.parent_id)
        by_title.setdefault(pp.path[-1], []).append(c)
    assert {(c.start_s, c.end_s) for c in by_title["Intro"]} == {(0.0, 75.0)}
    assert {(c.start_s, c.end_s) for c in by_title["Pods"]} == {(75.0, 3700.0)}
    assert {c.start_s for c in by_title["Wrap up"]} == {3700.0}
    assert all(c.end_s is None for c in by_title["Wrap up"])


def test_ids_are_stable():
    md = f"# S\n\n{_sentences(60)}\n\n## T\n\n{_sentences(30, 'x')}\n"
    a = _chunk(md)
    b = _chunk(md)
    assert [p.id for p in a[0]] == [p.id for p in b[0]]
    assert [c.id for c in a[1]] == [c.id for c in b[1]]
    assert len({c.id for c in a[1]}) == len(a[1])
    other = chunk_markdown(md, doc_title="Doc", doc_key="doc-2", embedder=EMB)
    assert not ({c.id for c in other[1]} & {c.id for c in a[1]})
