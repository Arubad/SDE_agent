"""Manual live check that prompt caching works on Foundry (never run by tests; costs well under $0.01).

    backend/venv/bin/python -m backend.tools.check_prompt_cache

Sends two identical `haiku_json` calls whose system prompt is over the 512-token minimum cacheable prefix, prints
both usages, and exits 1 if the second call read nothing from the cache.
"""
from __future__ import annotations

import json
import sys

from backend.core import config, llm, trace as trace_mod
from backend.core.trace import Trace

# ~80 words per paragraph, 12 paragraphs: comfortably over 600 tokens, and fixed text so the prefix is identical
_PARAGRAPH = ("You are a careful software engineering tutor. You explain distributed systems, data structures, "
              "algorithms, testing, code review and operating systems in plain language. You prefer concrete "
              "examples, name the trade-offs explicitly, and point out common mistakes learners make. When a "
              "question is ambiguous you state the assumption you are making. You never invent citations, and "
              "you keep answers short unless the learner asks for depth. Topic {i} of the fixed curriculum. ")
SYSTEM = "".join(_PARAGRAPH.format(i=i) for i in range(1, 13))
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"],
          "properties": {"answer": {"type": "string"}}}
USAGE_KEYS = ("model", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
              "cache_write_5m_tokens", "cache_write_1h_tokens")


def _call() -> dict:
    trace = Trace("check_prompt_cache")
    token = trace_mod._current.set(trace)
    try:
        llm.haiku_json(system=SYSTEM, prompt="In one sentence: what is a cache?", schema=SCHEMA, max_tokens=200)
    finally:
        trace_mod._current.reset(token)
    attrs = trace.spans[-1].attrs
    return {k: attrs[k] for k in USAGE_KEYS if k in attrs}


def main() -> int:
    if not config.PROMPT_CACHE:
        print("PROMPT_CACHE is false; set it to true to check caching")
        return 1
    first, second = _call(), _call()
    print("first: ", json.dumps(first))
    print("second:", json.dumps(second))
    # cache_read_tokens is the SDK's usage.cache_read_input_tokens (None counted as 0)
    if second.get("cache_read_tokens", 0) == 0:
        print("FAIL: second call has cache_read_input_tokens == 0")
        return 1
    print("OK: second call read", second["cache_read_tokens"], "tokens from the cache")
    return 0


if __name__ == "__main__":
    sys.exit(main())
