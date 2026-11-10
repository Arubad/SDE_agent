from backend.agent.guard import allowed_tool_call, sanitize_links, user_video_ids

VID = "aircAruvnKk"

def test_user_video_ids_come_from_user_turns_only():
    history = [{"role": "user", "parts": [{"text": f"watch https://youtu.be/{VID}"}]},
               {"role": "model", "parts": [{"text": "see https://youtu.be/zzzzzzzzzz1"}]}]
    assert user_video_ids("what did it say about neurons?", history) == {VID}
    assert user_video_ids("https://www.youtube.com/watch?v=dQw4w9WgXcQ", []) == {"dQw4w9WgXcQ"}

def test_youtube_tool_only_for_user_supplied_videos():
    kw = dict(user_video_ids={VID}, seen_doc_ids=set())
    assert allowed_tool_call("analyze_youtube_video", {"video_url": f"https://youtu.be/{VID}"}, **kw) is None
    assert "not provided by the user" in allowed_tool_call("analyze_youtube_video", {"video_url": "https://youtu.be/zzzzzzzzzz1"}, **kw)
    assert allowed_tool_call("analyze_youtube_video", {"video_url": "garbage"}, **kw) is not None

def test_get_document_only_for_seen_documents_or_user_videos():
    kw = dict(user_video_ids={VID}, seen_doc_ids={"doc-1"})
    assert allowed_tool_call("get_document", {"doc_id": "doc-1"}, **kw) is None
    assert allowed_tool_call("get_document", {"doc_id": f"youtube:{VID}"}, **kw) is None
    assert allowed_tool_call("get_document", {"doc_id": "youtube:zzzzzzzzzz1"}, **kw) is not None

def test_search_allowed_and_unknown_tools_refused():
    kw = dict(user_video_ids=set(), seen_doc_ids=set())
    assert allowed_tool_call("search_knowledge_base", {"query": "x"}, **kw) is None
    assert "unknown tool" in allowed_tool_call("delete_everything", {}, **kw)

def test_sanitize_links_removes_untrusted_urls_only():
    ok = {"https://github.com/a/b"}
    assert sanitize_links("see [docs](https://github.com/a/b/) and [x](https://evil.example/?q=1)", ok) == \
        "see [docs](https://github.com/a/b/) and x"
    assert sanitize_links("before ![p](https://evil.example/i.png?d=secret) after", ok) == "before  after"

def test_sanitize_links_leaves_code_and_citations_alone():
    code = "run `curl https://evil.example` or\n```\n[x](https://evil.example)\n```"
    assert sanitize_links(code, set()) == code
    assert sanitize_links("claim[1][2] and <sup>[3]</sup>", set()) == "claim[1][2] and <sup>[3]</sup>"
