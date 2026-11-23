import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.mcp.servers.learner import build_learner_server
from backend.tutor.repo import create_roadmap

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _skill(sid, prereqs=()):
    return {"id": sid, "title": sid.upper(), "objectives": [], "prerequisites": list(prereqs)}


SPEC = {"title": "T", "goals": [], "weekly_hours": None, "stages": [
    {"id": "s1", "title": "S1", "timeframe": None, "summary": "", "resources": [], "practice_goals": [],
     "skills": [_skill("a"), _skill("b", ["a"])]}]}


@pytest.fixture
def clean(engine):
    with session_scope(engine) as s:
        s.execute(text("DELETE FROM roadmaps"))
        s.execute(text("DELETE FROM learner_facts"))
        s.execute(text("UPDATE learner_settings SET consent_personal = NULL, consent_raw_messages = NULL"))


@pytest.fixture
def server(engine, clean):
    return build_learner_server(engine, HashEmbedder())


def _consent(engine, value):
    with session_scope(engine) as s:
        s.execute(text("UPDATE learner_settings SET consent_personal = :v WHERE id = 1"), {"v": value})


def _roadmap(engine, slug="rm", spec=SPEC):
    with session_scope(engine) as s:
        rm = create_roadmap(s, slug=slug, title=slug.upper(), source_text="x")
        rm.spec = spec


async def _call(server, tool, **args):
    async with Client(server) as c:
        return (await c.call_tool(tool, args)).data


def _count(engine):
    with session_scope(engine) as s:
        return s.execute(text("SELECT count(*) FROM learner_facts")).scalar()


@pytest.mark.parametrize("value", [None, False])
async def test_remember_refused_without_consent(engine, server, value):
    _consent(engine, value)
    with pytest.raises(ToolError, match="Personal memory is off"):
        await _call(server, "remember", text="I like Go")
    assert _count(engine) == 0


async def test_remember_redacts_before_storing(engine, server):
    _consent(engine, True)
    await _call(server, "remember", text="my email is a@b.co")
    with session_scope(engine) as s:
        stored = s.execute(text("SELECT text FROM learner_facts")).scalar()
    assert "a@b.co" not in stored


async def test_near_duplicate_updates_instead_of_inserting(engine, server):
    _consent(engine, True)
    base = "the learner prefers short worked examples with plenty of code and diagrams every lesson"
    first = await _call(server, "remember", text=base)
    second = await _call(server, "remember", text=base + " please")
    assert first["action"] == "inserted" and second["action"] == "updated"
    assert second["id"] == first["id"] and _count(engine) == 1
    with session_scope(engine) as s:
        prov = s.execute(text("SELECT provenance FROM learner_facts")).scalar()
    assert prov["history"][0]["text"] == base
    other = await _call(server, "remember", text="goal is passing the kubernetes certification exam in June")
    assert other["action"] == "inserted" and _count(engine) == 2


async def test_kind_and_roadmap_separate_facts(engine, server):
    _consent(engine, True)
    _roadmap(engine)
    t = "the learner prefers short worked examples with plenty of code and diagrams every lesson"
    await _call(server, "remember", text=t)
    assert (await _call(server, "remember", text=t, kind="goal"))["action"] == "inserted"
    assert (await _call(server, "remember", text=t, roadmap="rm"))["action"] == "inserted"
    with pytest.raises(ToolError, match="No roadmap 'nope'"):
        await _call(server, "remember", text=t, roadmap="nope")


async def test_recall_scoping_and_list(engine, server):
    _consent(engine, True)
    _roadmap(engine)
    await _call(server, "remember", text="global note about pointers and memory layout")
    await _call(server, "remember", text="roadmap note about services and ingress", roadmap="rm")
    glob = await _call(server, "recall", query="pointers memory layout")
    assert [r["text"] for r in glob] == ["global note about pointers and memory layout"]
    assert 0.5 < glob[0]["score"] < 1
    both = await _call(server, "recall", query="services ingress", roadmap="rm")
    assert len(both) == 2 and both[0]["text"].startswith("roadmap note")
    assert len(await _call(server, "list_facts")) == 1
    assert len(await _call(server, "list_facts", roadmap="rm")) == 1


async def test_forget_really_forgets(engine, server):
    _consent(engine, True)
    t = "the learner works nights and studies on weekends"
    fid = (await _call(server, "remember", text=t))["id"]
    assert (await _call(server, "forget", fact_id=fid)) == {"deleted": True}
    assert await _call(server, "recall", query=t) == []
    assert _count(engine) == 0
    assert (await _call(server, "forget", fact_id=fid)) == {"deleted": False}


async def test_locked_until_prerequisite_reaches_threshold(engine, server):
    _roadmap(engine)
    first = await _call(server, "get_progress", roadmap="rm")
    assert [p["skill_id"] for p in first] == ["a", "b"] and first[0]["title"] == "A"
    assert first[0]["mastery"] == 0 and first[1]["locked"] is True and first[0]["locked"] is False
    await _call(server, "set_mastery", roadmap="rm", skill_id="a", mastery=0.69)
    assert (await _call(server, "get_progress", roadmap="rm"))[1]["locked"] is True
    await _call(server, "set_mastery", roadmap="rm", skill_id="a", mastery=0.7)
    assert (await _call(server, "get_progress", roadmap="rm"))[1]["locked"] is False
    # a per-skill threshold of 0.5 is honoured
    await _call(server, "set_mastery", roadmap="rm", skill_id="a", mastery=0.55)
    await _call(server, "set_mastery", roadmap="rm", skill_id="b", mastery=0)
    with session_scope(engine) as s:
        s.execute(text("UPDATE skill_progress SET unlock_threshold = 0.5 WHERE skill_id = 'b'"))
    assert (await _call(server, "get_progress", roadmap="rm"))[1]["locked"] is False


async def test_set_mastery_bounds_and_unknown_skill(engine, server):
    _roadmap(engine)
    out = await _call(server, "set_mastery", roadmap="rm", skill_id="a", mastery=0.4, weak_spots=["x"])
    assert out["skill_id"] == "a" and out["mastery"] == 0.4 and out["weak_spots"] == ["x"]
    kept = await _call(server, "set_mastery", roadmap="rm", skill_id="a", mastery=0.5)
    assert kept["weak_spots"] == ["x"] and kept["next_review_at"] is None
    for bad in (1.1, -0.1):
        with pytest.raises(ToolError):
            await _call(server, "set_mastery", roadmap="rm", skill_id="a", mastery=bad)
    with pytest.raises(ToolError, match="skill"):
        await _call(server, "set_mastery", roadmap="rm", skill_id="zzz", mastery=0.5)


async def test_progress_needs_approved_spec(engine, server):
    _roadmap(engine, slug="nospec", spec=None)
    with pytest.raises(ToolError, match="has no approved spec"):
        await _call(server, "get_progress", roadmap="nospec")


async def test_foreign_dimension_facts_never_break_or_leak(engine, server):
    _consent(engine, True)
    with session_scope(engine) as s:
        s.execute(text("INSERT INTO learner_facts (id, kind, text, embedding, embedder) VALUES "
                       "(gen_random_uuid(), 'fact', 'foreign', '[1,0,0]', 'Other:3')"))
    t = "the learner prefers short worked examples with plenty of code and diagrams every lesson"
    assert (await _call(server, "remember", text=t))["action"] == "inserted"
    assert (await _call(server, "remember", text=t + " please"))["action"] == "updated"
    hits = await _call(server, "recall", query=t, top_k=20)
    assert hits and all(h["text"] != "foreign" for h in hits)
