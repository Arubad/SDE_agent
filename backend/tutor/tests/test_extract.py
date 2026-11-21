from pathlib import Path

from backend.tutor.ingest.discover import discover
from backend.tutor.ingest.extract import extract_page

FIX = Path(__file__).parent / "fixtures" / "html"
DOCS_URL = "https://kubernetes.io/docs/concepts/"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_extract_keeps_structure():
    page = extract_page(_read("docs_page.html"), DOCS_URL)
    md = page.markdown
    assert page.lang == "en"
    assert page.word_count > 50
    assert "# Pods" in md and "## Using Pods" in md
    assert "```" in md
    assert "apiVersion: v1\nkind: Pod\nmetadata:\n  name: nginx" in md
    assert "image: nginx:1.14.2" in md
    assert "| Pending |" in md
    for sentinel in ("NAV_SENTINEL", "FOOTER_SENTINEL", "COOKIEBANNER_SENTINEL"):
        assert sentinel not in md


def test_extract_fallback_when_sparse():
    html = "<html><body><nav>NAVX</nav><h1>T</h1><p>short</p><pre>a = 1\nb = 2</pre></body></html>"
    page = extract_page(html, "https://x.test/")
    assert "a = 1\nb = 2" in page.markdown
    assert "```" in page.markdown and "NAVX" not in page.markdown


def test_discover_course_page():
    d = discover(_read("course_page.html"), "https://courses.example.com/c/")
    assert d.videos == ["aaaaaaaaaa1", "bbbbbbbbbb2", "ccccccccccc"]
    assert d.playlists == ["PLabcdefghij123"]


def test_discover_data_attrs_ldjson_and_shorts():
    html = (
        '<div data-video-id="ccccccccccc"></div>'
        '<div data-src="https://www.youtube.com/embed/ddddddddddd"></div>'
        '<script type="application/ld+json">{"@type":"VideoObject",'
        '"embedUrl":"https://www.youtube.com/embed/eeeeeeeeeee"}</script>'
        '<a href="https://www.youtube.com/shorts/fffffffffff">s</a>'
    )
    d = discover(html, "https://a.test/")
    assert d.videos == ["ccccccccccc", "ddddddddddd", "eeeeeeeeeee", "fffffffffff"]


def test_embed_with_list_and_video_id():
    d = discover(
        '<iframe src="https://www.youtube.com/embed/aaaaaaaaaa1?list=PLxyz"></iframe>',
        "https://a.test/",
    )
    assert d.videos == ["aaaaaaaaaa1"] and d.playlists == ["PLxyz"]


def test_discover_blog_assets():
    d = discover(_read("blog_page.html"), "https://blog.example.com/posts/one/")
    assert d.pdfs == ["https://blog.example.com/files/paper.pdf"]
    assert d.repos == ["octocat/hello-world"]


def test_focus_links_first():
    d = discover(_read("docs_page.html"), DOCS_URL, focus=["Pods"])
    assert d.links[0] == "https://kubernetes.io/docs/concepts/workloads/pods/"
    assert "https://kubernetes.io/docs/concepts/services/service/" in d.links
    # shallower comes before deeper among non-focus links
    assert d.links.index("https://kubernetes.io/docs/concepts/workloads/") < d.links.index(
        "https://kubernetes.io/docs/concepts/services/service/"
    )
    # no fragment duplicates, no assets, outside root dropped
    assert len(d.links) == len(set(d.links))
    assert not any(u.endswith(".png") or "#" in u or "/blog/" in u for u in d.links)


def test_offsite_links_ignored():
    d = discover(_read("docs_page.html"), DOCS_URL)
    assert not any("other.example.com" in u for u in d.links)


def test_explicit_root():
    d = discover(_read("docs_page.html"), DOCS_URL, root="/docs/")
    assert "https://kubernetes.io/docs/" in d.links


def test_malformed_input_does_not_raise():
    html = (
        '<a href="http://[abc">x</a><a>no href</a><a href="">e</a><a href="mailto:a@b.c">m</a>'
        '<iframe></iframe><iframe src="http://[bad"></iframe><div data-src="http://[x"></div>'
        '<div data-video-id=""></div><a href="https://github.com/">g</a>'
        '<script type="application/ld+json">{not json</script>'
        '<script type="application/ld+json">[1, {"embedUrl": 5}]</script><p><b>unclosed'
    )
    d = discover(html, "https://a.test/x/")
    assert d.videos == [] and d.pdfs == [] and d.repos == []
    discover(html, "http://[abc")
    assert extract_page(html, "http://[abc").markdown is not None
    assert extract_page("", "https://a.test/").markdown == ""
