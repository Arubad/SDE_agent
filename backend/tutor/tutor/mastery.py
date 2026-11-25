"""Mastery arithmetic for the assessor: a moving average of quiz scores, review dates and weak-spot upkeep."""
from __future__ import annotations

from datetime import datetime, timedelta

ALPHA = 0.35
MAX_WEAK_SPOTS = 10
WEAK_BELOW = 0.5  # a question scored under this adds its concept to the weak spots
CLEARED_AT = 0.8  # a question scored at or over this removes its concept


def update_mastery(old: float, score: float) -> float:
    return min(1.0, max(0.0, old + ALPHA * (score - old)))


def next_review(mastery: float, now: datetime) -> datetime:
    days = 1 if mastery < 0.5 else 3 if mastery < 0.7 else 7 if mastery < 0.85 else 14
    return now + timedelta(days=days)


def update_weak_spots(old: list[str], graded: list[dict]) -> list[str]:
    """`graded` items carry `concept` and `score`. Concepts scored below WEAK_BELOW are added (at the end), those
    scored at least CLEARED_AT removed; no duplicates; the newest MAX_WEAK_SPOTS are kept."""
    spots = list(dict.fromkeys(old))
    for g in graded:
        concept, score = g.get("concept"), g["score"]
        if not concept:
            continue
        if score < WEAK_BELOW:
            if concept in spots:
                spots.remove(concept)
            spots.append(concept)
        elif score >= CLEARED_AT and concept in spots:
            spots.remove(concept)
    return spots[-MAX_WEAK_SPOTS:]
