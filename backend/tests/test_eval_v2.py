import json
from backend.eval import common
from backend.eval.generate_questions_v2 import build_record, validate_multi_hop

A = "Consistent hashing maps keys and servers onto a ring so that adding a server moves only a small share of keys."
B = "Virtual nodes place each physical server at many ring positions, which evens out load between servers."
OUT = {"suitable": True, "question": "Q?", "reference_answer": "R.",
       "evidence_quote_a": "adding a server moves only a small share of keys",
       "evidence_quote_b": "place each physical server at many ring positions"}

def test_validate_multi_hop_requires_both_quotes_verbatim():
    assert validate_multi_hop(OUT, A, B)
    assert not validate_multi_hop({**OUT, "evidence_quote_b": "servers are balanced by some other trick entirely"}, A, B)
    assert not validate_multi_hop({**OUT, "evidence_quote_a": "a small share"}, A, B)   # under 6 words
    assert not validate_multi_hop({**OUT, "suitable": False}, A, B)

def test_build_record_is_deterministic():
    ev = [{"quote": "q1", "doc_id": "d1", "title": "t1"}, {"quote": "q2", "doc_id": "d2", "title": "t2"}]
    r = build_record("multi_hop", "Q?", reference_answer="R.", evidence=ev)
    assert r == build_record("multi_hop", "Q?", reference_answer="R.", evidence=ev)
    assert len(r["id"]) == 10 and r["split"] in ("dev", "test") and r["topic"] == ""
    u = build_record("unanswerable", "U?", topic="Raft")
    assert u["evidence"] == [] and u["reference_answer"] == "" and u["topic"] == "Raft"

def test_load_questions_v2_filters(tmp_path, monkeypatch):
    rows = [build_record("multi_hop", f"Q{i}?", reference_answer="R", evidence=[]) for i in range(6)]
    rows.append(build_record("unanswerable", "U?", topic="Raft"))
    path = tmp_path / "q.jsonl"; path.write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(common, "QUESTIONS_V2_PATH", path)
    assert len(common.load_questions_v2()) == 7
    assert [r["kind"] for r in common.load_questions_v2(kind="unanswerable")] == ["unanswerable"]
    assert all(r["split"] == "dev" for r in common.load_questions_v2(split="dev"))
