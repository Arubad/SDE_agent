import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from backend.tutor.db import session_scope
from backend.tutor.ingest.embed import HashEmbedder
from backend.tutor.learner import store
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


async def test_remember_length_capped(engine, server):
    _consent(engine, True)
    with pytest.raises(ToolError, match="at most 1000"):
        await _call(server, "remember", text="x" * 1001)
    assert (await _call(server, "remember", text="y" * 1000))["action"] == "inserted"


async def test_recall_and_list_empty_without_consent(engine, server):
    _consent(engine, True)
    _roadmap(engine)
    await _call(server, "remember", text="likes videos")
    await _call(server, "remember", text="likes labs", roadmap="rm")
    _consent(engine, False)
    assert await _call(server, "recall", query="videos") == []
    assert await _call(server, "recall", query="labs", roadmap="rm") == []
    assert await _call(server, "list_facts") == [] and await _call(server, "list_facts", roadmap="rm") == []
    assert _count(engine) == 2  # hidden, not deleted: purging is the Memory API's job
    _consent(engine, True)
    assert len(await _call(server, "list_facts")) == 1


def test_consented_means_personal_consent(engine, clean):
    assert store.consented(engine) is False
    _consent(engine, True)
    assert store.consented(engine) is True and store.get_settings(engine).consent_raw_messages is None
    _consent(engine, False)
    assert store.consented(engine) is False


def test_purge_facts_deletes_all(engine, clean):
    _consent(engine, True)
    emb = HashEmbedder()
    store.remember(engine, emb, "likes videos about networking", "fact", None)
    store.remember(engine, emb, "wants the kubernetes certification", "goal", None)
    _consent(engine, False)  # purge neither checks nor changes consent
    assert store.purge_facts(engine) == 2 and _count(engine) == 0
    assert store.consented(engine) is False and store.purge_facts(engine) == 0


def test_update_fact_redacts_reembeds_and_keeps_history(engine, clean):
    emb = HashEmbedder()
    _consent(engine, True)
    fid = store.remember(engine, emb, "prefers long lectures", "preference", None)["id"]
    out = store.update_fact(engine, emb, fid, "prefers short labs, mail a@b.co", "goal")
    assert out == {"id": fid, "action": "updated"}
    with session_scope(engine) as s:
        row = s.execute(text("SELECT kind, text, provenance, embedder FROM learner_facts")).one()
    assert row.kind == "goal" and "a@b.co" not in row.text and row.embedder == store.embedder_name(emb)
    assert row.provenance["history"][0]["text"] == "prefers long lectures"
    assert [h["text"] for h in store.recall(engine, emb, "short labs", 1, None)] == [row.text]
    with pytest.raises(ValueError, match="at most 1000"):
        store.update_fact(engine, emb, fid, "x" * 1001, "fact")
    with pytest.raises(ValueError, match="No fact"):
        store.update_fact(engine, emb, "00000000-0000-0000-0000-000000000000", "t", "fact")
    with pytest.raises(ValueError, match="No fact 'nope'"):
        store.update_fact(engine, emb, "nope", "t", "fact")
    _consent(engine, False)
    with pytest.raises(ValueError, match="Personal memory is off"):
        store.update_fact(engine, emb, fid, "t", "fact")


def test_set_mastery_next_review_at_and_provenance(engine, clean):
    from datetime import datetime, timezone
    _roadmap(engine)
    due = datetime(2026, 12, 1, tzinfo=timezone.utc)
    out = store.set_mastery(engine, "rm", "a", 0.5, None, next_review_at=due)
    assert out["next_review_at"] == due.isoformat()
    assert store.set_mastery(engine, "rm", "a", 0.6, None)["next_review_at"] == due.isoformat()  # kept
    _consent(engine, True)
    fid = store.remember(engine, HashEmbedder(), "curated note", "fact", None, {"source": "curator"})["id"]
    with session_scope(engine) as s:
        assert s.execute(text("SELECT provenance FROM learner_facts")).scalar() == {"source": "curator"}
    assert store.skills(SPEC)[1]["id"] == "b" and fid
