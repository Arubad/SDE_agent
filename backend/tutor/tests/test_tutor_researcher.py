"""The researcher (Task 11): web research to a mini roadmap, two approvals, ingestion through the gated tools."""
import re

import pytest

from backend.core import fake_llm
from backend.core.fake_llm import FakeReply
from backend.tutor.db import session_scope
from backend.tutor.ingest.fetch import public_resolver
from backend.tutor.models import Roadmap
from backend.tutor.repo import SLUG_RE, create_roadmap, get_roadmap_by_slug
from backend.tutor.tests.tutor_helpers import (
    FakeGateway,
    clear_memo,  # noqa: F401 (fixture)
    interrupts,
    make_runtime,
    new_session,
    nodes,
    replies,
    run,
)
from backend.tutor.roadmap.parse import extract_urls
from backend.tutor.tutor.runtime import BadResume
from backend.tutor.tutor.researcher import MAX_RESOURCES, RESEARCH_SYSTEM, count_resources, slug_for, strip_links
from backend.tutor.untrusted import wrap

pytestmark = pytest.mark.anyio

DEFS = [{"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}}
        for n in ("web__search", "kb__search", "ingest__create_roadmap")]
GOOD = "https://k8s.example/probes"
MD = ("# K8s Ops\n## Basics\n### Probes\nHealth checks.\n- [Probes](" + GOOD + ")\n"
      "- [Bad](https://evil.example/x)\n### Pods\n- [Pods](" + GOOD + "/pods)\n")
PARSED = {"title": "K8s Ops", "goals": [], "weekly_hours": None, "stages": [
    {"id": "s1", "title": "Basics", "timeframe": None, "summary": "", "practice_goals": [],
     "resources": [{"id": "r1", "title": "Probes", "url": GOOD}, {"id": "r2", "title": "Pods", "url": GOOD + "/pods"}],
     "skills": [{"id": "probes", "title": "Probes", "objectives": [], "prerequisites": []}]}]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _search(*urls):
    return {"data": {"results": [{"url": wrap(u), "title": wrap("t"), "snippet": wrap("s"), "engine": wrap("fake")}
                                 for u in urls]}}


def _gateway(engine, create_error=None, approve_error=None):
    def create(args):
        if create_error:
            return {"error": wrap(create_error)}
        with session_scope(engine) as s:
            rm = create_roadmap(s, slug=args["slug"], title=args["title"], source_text=args["source_text"])
            rm.spec, rm.status = PARSED, "parsed"
        return {"data": {"status": wrap("parsed"), "slug": wrap(args["slug"])}}

    return FakeGateway(
        defs={"researcher": DEFS}, ask=("ingest.create_roadmap", "ingest.approve_roadmap"),
        results={"web__search": _search(GOOD, GOOD + "/pods"), "ingest.create_roadmap": create,
                 "ingest.approve_roadmap": ({"error": wrap(approve_error)} if approve_error
                                            else {"data": {"status": wrap("approved")}})})


def _rt(engine, gw):
    return make_runtime(engine, gateway=gw, url_resolver=public_resolver)


def _research(md=MD):
    return fake_llm.script([FakeReply(tool_uses=[("web__search", {"query": "probes"})]), FakeReply(text=md)])


async def _answer(rt, sid, approved=True):
    """Answer the pending card (its id is part of the answer, R10)."""
    return await run(rt, sid, {"resume": {"approved": approved, "card": (await rt.pending(sid))["id"]}})


async def _to_first_card(rt, sid, topic="kubernetes probes"):
    with _research():
        return await run(rt, sid, {"action": "research", "topic": topic})


async def test_unseen_urls_stripped_from_proposal(engine, clear_memo):  # noqa: F811
    gw = _gateway(engine)
    rt, sid = _rt(engine, gw), new_session(engine)
    events = await _to_first_card(rt, sid)
    (card,) = interrupts(events)
    src = card["payload"]["args"]["source_text"]
    assert GOOD in src and "evil.example" not in src and "Bad" in src
    assert nodes(events) == ["coordinator", "research", "research_approve_create"]
    assert gw.released == [sid]
    assert card["summary"] == "New roadmap 'K8s Ops': 2 skills, 2 resources"
    args = card["payload"]["args"]
    assert card["payload"]["tool"] == "ingest.create_roadmap" and set(args) == {"slug", "title", "source_text",
                                                                              "idempotency_key"}
    assert args["slug"] == "k8s-ops" and args["title"] == "K8s Ops" and args["idempotency_key"].endswith(":create")
    assert gw.calls and all(c[0] == "web__search" for c in gw.calls)  # nothing created yet
    request = fake_llm.requests[0]
    assert request["messages"] == [{"role": "user", "content":
                                    "Research this topic and build the roadmap: kubernetes probes"}]
    assert request["system"][0]["text"] == RESEARCH_SYSTEM
    assert not any("ingest" in t["name"] for t in request["tools"])


async def test_two_approvals_then_ingest_through_gateway(engine, clear_memo):  # noqa: F811
    gw = _gateway(engine)
    rt, sid = _rt(engine, gw), new_session(engine)
    (card,) = interrupts(await _to_first_card(rt, sid))
    events = await _answer(rt, sid, True)
    (card2,) = interrupts(events)
    assert nodes(events) == ["research_approve_create", "research_create", "research_approve_ingest"]
    slug = card["payload"]["args"]["slug"]
    key = card["payload"]["args"]["idempotency_key"].removesuffix(":create")
    assert card2 == {"kind": "approval", "id": card2["id"], "action": "approve_roadmap", "summary": "Ingest 'K8s Ops' (2 resources)",
                     "payload": {"tool": "ingest.approve_roadmap",
                                 "args": {"slug": slug, "idempotency_key": f"{key}:approve"}, "spec": PARSED}}
    events = await _answer(rt, sid, True)
    assert replies(events) == ["Ingesting 'K8s Ops'. It will appear under Roadmaps; "
                               "I'll keep teaching from the web meanwhile."]
    assert [e for e in events if e["type"] == "roadmap_created"] == [{"type": "roadmap_created", "slug": slug}]
    assert [(n, a) for n, _, a in gw.approvals] == [("ingest.create_roadmap", True), ("ingest.approve_roadmap", True)]
    assert [c[0] for c in gw.calls][-2:] == ["ingest.create_roadmap", "ingest.approve_roadmap"]
    assert gw.calls[-1][2].agent == "researcher"
    state = await rt.state(sid)
    assert state.get("proposal") is None
    assert [m["role"] for m in state["messages"]] == ["user", "assistant"]
    assert state["messages"][1]["content"].startswith("Ingesting")
    assert "source_text" not in str(state["messages"])


async def test_approver_refuses_changed_args(engine, clear_memo):  # noqa: F811
    gw = _gateway(engine)
    rt, sid = _rt(engine, gw), new_session(engine)
    await _to_first_card(rt, sid)
    await _answer(rt, sid, True)
    await _answer(rt, sid, True)
    for name, payload, answer in gw.approvals:
        assert answer is True
        approver = next(c[2] for c in gw.calls if c[0] == name).approve
        assert approver(payload) is True
        assert approver({**payload, "args": {**payload["args"], "slug": "other"}}) is False
        assert approver({**payload, "args": {}}) is False


async def test_decline_creates_nothing(engine, clear_memo):  # noqa: F811
    gw = _gateway(engine)
    rt, sid = _rt(engine, gw), new_session(engine)
    (card,) = interrupts(await _to_first_card(rt, sid))
    events = await _answer(rt, sid, False)
    assert replies(events) == ["OK, nothing was created."]
    assert not any(c[0].startswith("ingest") for c in gw.calls) and gw.approvals == []
    with session_scope(engine) as s:
        assert get_roadmap_by_slug(s, card["payload"]["args"]["slug"]) is None
    state = await rt.state(sid)
    assert state.get("proposal") is None and await rt.pending(sid) is None


async def test_decline_at_second_card_keeps_draft(engine, clear_memo):  # noqa: F811
    gw = _gateway(engine)
    rt, sid = _rt(engine, gw), new_session(engine)
    (card,) = interrupts(await _to_first_card(rt, sid))
    await _answer(rt, sid, True)
    events = await _answer(rt, sid, False)
    assert replies(events) == ["OK, nothing was ingested. 'K8s Ops' stays in Roadmaps as a draft you can "
                               "approve later."]
    assert [c[0] for c in gw.calls if c[0].startswith("ingest")] == ["ingest.create_roadmap"]
    with session_scope(engine) as s:
        assert get_roadmap_by_slug(s, card["payload"]["args"]["slug"]) is not None
    assert (await rt.state(sid)).get("proposal") is None


async def test_create_error_ends_the_turn(engine, clear_memo):  # noqa: F811
    rt, sid = _rt(engine, _gateway(engine, create_error="boom")), new_session(engine)
    await _to_first_card(rt, sid)
    events = await _answer(rt, sid, True)
    assert replies(events) == ["I couldn't create the roadmap: boom"] and not interrupts(events)
    assert (await rt.state(sid)).get("proposal") is None


async def test_ingest_error_is_reported(engine, clear_memo):  # noqa: F811
    rt, sid = _rt(engine, _gateway(engine, approve_error="queue down")), new_session(engine)
    await _to_first_card(rt, sid)
    await _answer(rt, sid, True)
    events = await _answer(rt, sid, True)
    assert replies(events) == ["I couldn't start ingesting 'K8s Ops': queue down"]
    assert not [e for e in events if e["type"] == "roadmap_created"]


@pytest.mark.parametrize("turn", [{"action": "research"}, {"action": "research", "topic": "   "}])
async def test_no_topic_asks(engine, clear_memo, turn):  # noqa: F811
    rt, sid = _rt(engine, _gateway(engine)), new_session(engine)
    events = await run(rt, sid, turn)
    assert replies(events) == ["Tell me what you'd like to learn, and I'll research it."]
    assert fake_llm.requests == [] and not interrupts(events)


async def test_empty_research_says_so(engine, clear_memo):  # noqa: F811
    rt, sid = _rt(engine, _gateway(engine)), new_session(engine)
    with fake_llm.script([FakeReply(text="# Nothing\nI found nothing.")]):
        events = await run(rt, sid, {"action": "research", "topic": "zzz"})
    assert replies(events) == ["I couldn't find enough to build a roadmap for that; try a more specific topic."]
    assert not interrupts(events)


async def test_text_route_uses_text_as_topic(engine, clear_memo):  # noqa: F811
    rt, sid = _rt(engine, _gateway(engine)), new_session(engine)
    with fake_llm.script([FakeReply(json={"route": "new_topic"}), FakeReply(text=MD)]):
        events = await run(rt, sid, {"text": "teach me kubernetes"})
    assert len(interrupts(events)) == 1
    assert fake_llm.requests[-1]["messages"][0]["content"].endswith("teach me kubernetes")


async def test_qa_mode_research_still_goes_to_researcher(engine, clear_memo):  # noqa: F811
    rt, sid = _rt(engine, _gateway(engine)), new_session(engine, mode="qa")
    events = await _to_first_card(rt, sid)
    assert nodes(events) == ["coordinator", "research", "research_approve_create"]


async def test_release_on_failure(engine, clear_memo):  # noqa: F811
    gw = _gateway(engine)
    rt, sid = _rt(engine, gw), new_session(engine)
    gw.results["web__search"] = lambda a: 1 / 0
    with _research(), pytest.raises(ZeroDivisionError):
        await run(rt, sid, {"action": "research", "topic": "x"})
    assert gw.released == [sid]


async def test_slug_for(engine):
    assert slug_for("Rust: Ownership & Borrowing!", engine) == "rust-ownership-borrowing"
    assert len(slug_for("a" * 80, engine)) == 50
    assert re.fullmatch(r"topic-[0-9a-f]{6}", slug_for("!!!", engine))
    assert re.fullmatch(r"topic-[0-9a-f]{6}", slug_for("é", engine))
    with session_scope(engine) as s:
        for slug in ("dup-slug", "dup-slug-2"):
            s.add(Roadmap(slug=slug, title="t", source_text="x"))
    assert slug_for("Dup Slug", engine) == "dup-slug-3"
    assert re.fullmatch(SLUG_RE, slug_for("x" * 60 + " y", engine))


class _Policy:
    def refusal(self, url):
        return "private" if "private" in url else None


async def test_strip_links_rules():
    ok = {"https://a.example/1", "https://private.example/x"}
    text = ("[A](https://a.example/1) and [B](https://b.example) see https://c.example/z. and https://a.example/1.\n"
            "[P](https://private.example/x)")
    out = await strip_links(text, ok, _Policy())
    assert out == "[A](https://a.example/1) and B see . and https://a.example/1.\nP"


STRIP_FORMS = [
    "- [see http://evil.example/x](https://a.example/1)",
    '- [T](https://a.example/1 "http://evil.example/y")',
    "- [T](<https://a.example/1> 'x') HTTPS://evil.example/x and Http://evil.example/y.",
    "- [HTTPS://evil.example/x](HTTPS://evil.example/x)",
    "- [T](https://a.example/1) ftp://evil.example/f and [U](javascript://evil.example/%0Aalert(1))",
    "- <HTTP://evil.example/z> [x]( https://a.example/1 )[y](http://evil.example/q)",
]


@pytest.mark.parametrize("text", STRIP_FORMS)
async def test_strip_links_leaves_only_allowed_urls(text):
    out = await strip_links(text, {"https://a.example/1"}, _Policy())
    assert "evil" not in out.lower()
    assert set(extract_urls(out)) <= {"https://a.example/1"}
    assert not re.findall(r"[a-z][a-z0-9+.-]*://(?!a\.example/1)", out, re.I)
    assert ("https://a.example/1" in out) == ("a.example" in text) and "\"" not in out and "'x'" not in out


async def test_strip_links_caps_resources_by_url_not_line():
    urls = {f"https://a.example/{i}" for i in range(20)}
    text = "### S\n- " + " ".join(f"[R{i}](https://a.example/{i})" for i in range(20)) + "\n- [R0](https://a.example/0)"
    out = await strip_links(text, urls, _Policy())
    assert len(extract_urls(out)) == MAX_RESOURCES == count_resources(out)
    assert "[R11](" in out and "[R12](" not in out and "R12" in out and out.startswith("### S")
    assert out.endswith("[R0](https://a.example/0)")  # a repeat of a kept URL adds no resource


async def test_card_counts_what_the_parser_will_make():
    md = "# T\n### S\n- [A](https://a.example/1) [B](https://a.example/2) https://a.example/3 [A](https://a.example/1)"
    assert count_resources(md) == 3


async def test_approval_answer_must_name_the_pending_card(engine, clear_memo):  # noqa: F811
    rt, sid = _rt(engine, _gateway(engine)), new_session(engine)
    (card,) = interrupts(await _to_first_card(rt, sid))
    old = {"resume": {"approved": True, "card": card["id"]}}
    await run(rt, sid, old)
    pending = await rt.pending(sid)
    assert pending["action"] == "approve_roadmap" and pending["id"] != card["id"]
    before = await rt.state(sid)
    for bad in (old, {"resume": {"approved": True}}, {"resume": {"approved": True, "card": 5}},
                {"resume": {"approved": "yes", "card": pending["id"]}},
                {"resume": {"approved": True, "card": pending["id"], "x": 1}}):
        with pytest.raises(BadResume):
            await run(rt, sid, bad)
    assert await rt.state(sid) == before and await rt.pending(sid) == pending
