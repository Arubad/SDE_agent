import pytest
from backend.eval.agent_eval import evidence_recall
from backend.eval.common import paired_bootstrap_delta

def test_bootstrap_delta():
    same = paired_bootstrap_delta([0, 1, 1, 0], [0, 1, 1, 0])
    assert same["delta"] == 0 and not same["significant"]
    up = paired_bootstrap_delta([0.0] * 20, [1.0] * 20)
    assert up["delta"] == 1.0 and up["ci95"] == [1.0, 1.0] and up["significant"]
    with pytest.raises(ValueError):
        paired_bootstrap_delta([1], [1, 2])

def test_evidence_recall_counts_quotes_found_anywhere_in_context():
    texts = ["Adding a server moves only a small share of keys on the ring.", "unrelated"]
    quotes = ["adding a server moves only a small share of keys", "virtual nodes even out load between servers nicely"]
    assert evidence_recall(quotes, texts) == 0.5
    assert evidence_recall([], texts) == 0.0
