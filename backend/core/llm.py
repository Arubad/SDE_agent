"""LLM clients: Claude Haiku 5.5 via Microsoft Foundry, Gemini via google-genai."""
from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Any

import anthropic
from anthropic import AnthropicFoundry

from backend.core import config
from backend.core.trace import current_trace


class LLMUnavailable(RuntimeError):
    """Raised when a provider is rate limited / out of quota / down, so callers can fall back."""


def _foundry_base_url() -> str:
    # The SDK wants ".../anthropic/"; people often paste the full ".../anthropic/v1/messages" URL
    base = config.FOUNDRY_ENDPOINT.split("/v1/")[0].rstrip("/")
    return base + "/"


@lru_cache(maxsize=1)
def haiku_client() -> AnthropicFoundry:
    if not config.FOUNDRY_API_KEY or not config.FOUNDRY_ENDPOINT:
        raise LLMUnavailable("Foundry credentials missing (TESTFORGE_FOUNDRY_API_KEY / TESTFORGE_FOUNDRY_ENDPOINT)")
    return AnthropicFoundry(api_key=config.FOUNDRY_API_KEY, base_url=_foundry_base_url(), max_retries=3, timeout=120)


def haiku_async_client() -> anthropic.AsyncAnthropicFoundry:
    """A new async client per call (streaming generation, agent loop); the caller closes it."""
    if not config.FOUNDRY_API_KEY or not config.FOUNDRY_ENDPOINT:
        raise LLMUnavailable("Foundry credentials missing (TESTFORGE_FOUNDRY_API_KEY / TESTFORGE_FOUNDRY_ENDPOINT)")
    return anthropic.AsyncAnthropicFoundry(api_key=config.FOUNDRY_API_KEY, base_url=_foundry_base_url(),
                                           max_retries=2, timeout=120)


def _record_usage(span, response) -> None:
    usage = getattr(response, "usage", None)
    if span is not None and usage is not None:
        span.set(model=getattr(response, "model", None), input_tokens=usage.input_tokens,
                 output_tokens=usage.output_tokens)


def haiku_json(*, system: str, prompt: str | list, schema: dict, effort: str = "low",
               max_tokens: int = 4000, span_name: str = "haiku_json") -> dict:
    """One structured-output call. Returns the parsed JSON object (guaranteed to match `schema`)."""
    trace = current_trace()
    with trace.span(span_name, kind="llm") as span:
        try:
            response = haiku_client().messages.create(
                model=config.HAIKU_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            raise LLMUnavailable(f"Haiku unavailable: {e}") from e
        _record_usage(span, response)
        trace.count_llm_call()
        if response.stop_reason == "refusal":
            raise LLMUnavailable("Haiku declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMUnavailable("Haiku structured output hit max_tokens")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)


# --- Gemini -------------------------------------------------------------------------

@lru_cache(maxsize=1)
def gemini_client():
    from google import genai
    if not config.GEMINI_API_KEY:
        raise LLMUnavailable("GEMINI_API_KEY missing")
    return genai.Client(api_key=config.GEMINI_API_KEY)


def _is_quota_error(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower() or "503" in msg


def gemini_generate(contents: Any, *, system: str | None = None, video: bool = False,
                    span_name: str = "gemini") -> str:
    from google.genai import types

    trace = current_trace()
    with trace.span(span_name, kind="llm") as span:
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW if video else None,
        )
        try:
            response = gemini_client().models.generate_content(model=config.GEMINI_MODEL, contents=contents, config=cfg)
        except Exception as e:  # google-genai raises its own error hierarchy
            if _is_quota_error(e):
                raise LLMUnavailable(f"Gemini unavailable: {str(e)[:200]}") from e
            raise
        trace.count_llm_call()
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            span.set(model=config.GEMINI_MODEL, input_tokens=meta.prompt_token_count,
                     output_tokens=meta.candidates_token_count)
        if not response.text:
            raise LLMUnavailable("Gemini returned an empty response")
        return response.text


def retry_after_seconds(e: Exception, default: float = 2.0) -> float:
    resp = getattr(e, "response", None)
    try:
        return float(resp.headers.get("retry-after", default)) if resp is not None else default
    except (TypeError, ValueError):
        return default


def with_backoff(fn, *, attempts: int = 3):
    """Retry a callable on LLMUnavailable with exponential backoff (used by offline eval scripts)."""
    for i in range(attempts):
        try:
            return fn()
        except LLMUnavailable:
            if i == attempts - 1:
                raise
            time.sleep(2 ** (i + 1))
