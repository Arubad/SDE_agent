"""A memo for deterministic model calls: the same inputs are answered from the `llm_memo` table."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import Engine, text

from backend.core import config
from backend.core.llm import haiku_json
from backend.core.trace import current_trace


def haiku_json_memo(engine: Engine, *, system: str, prompt: str, schema: dict, effort: str,
                    max_tokens: int = 4000, span_name: str) -> dict:
    key = hashlib.sha256(
        json.dumps([config.HAIKU_MODEL, system, prompt, schema, effort], sort_keys=True).encode()
    ).hexdigest()
    with engine.connect() as conn:
        hit = conn.execute(text("SELECT response FROM llm_memo WHERE key = :k"), {"k": key}).scalar()
    if hit is not None:
        with current_trace().span("memo_hit", kind="memo", span_name=span_name):
            pass
        return hit
    response = haiku_json(system=system, prompt=prompt, schema=schema, effort=effort,
                          max_tokens=max_tokens, span_name=span_name)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO llm_memo (key, response) VALUES (:k, CAST(:r AS jsonb)) ON CONFLICT DO NOTHING"),
            {"k": key, "r": json.dumps(response)},
        )
    return response
