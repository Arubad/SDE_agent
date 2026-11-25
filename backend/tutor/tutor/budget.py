"""Per-turn budgets and the retry policy for model calls."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from backend.core.llm import LLMUnavailable
from backend.core.trace import Trace

T = TypeVar("T")

CACHE_READ_WEIGHT = 0.1  # a cache read costs a tenth of fresh input, so it counts a tenth toward the token budget

OUTAGE_MESSAGE = "The model is unavailable right now. Your session is saved; try again in a minute."
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0)  # read at call time, so tests can make retries instant


class BudgetExceeded(Exception):
    def __init__(self, kind: str, used: str, limit: str):
        self.message = (f"This turn reached its {kind} budget ({used} of {limit}). "
                        "Your session is saved; ask again to continue.")
        super().__init__(self.message)


@dataclass(frozen=True)
class Budget:
    """`max_steps` is carried for callers: `run_tool_loop` enforces it, so `check` never raises for steps.

    The token budget counts `input + output + cache_write + 0.1 * cache_read`, rounded to an int (R20): a tool loop
    re-reads its cached prompt on every step, and counting those reads in full would end ordinary turns early. The
    dollar cap, which prices every kind, stays the hard limit."""

    max_steps: int
    max_tokens: int
    max_usd: float

    def check(self, trace: Trace) -> None:
        """Raise `BudgetExceeded` when the trace's weighted tokens or dollars are strictly over their limit."""
        s = trace.summary()
        tokens = round(s["input_tokens"] + s["output_tokens"] + s["cache_write_tokens"]
                       + CACHE_READ_WEIGHT * s["cache_read_tokens"])
        if tokens > self.max_tokens:
            raise BudgetExceeded("token", str(tokens), str(self.max_tokens))
        if s["cost_usd"] > self.max_usd:
            raise BudgetExceeded("cost", f"${s['cost_usd']:.4f}", f"${self.max_usd:.4f}")


async def with_llm_retry(fn: Callable[[], Awaitable[T]], *, delays: tuple[float, ...] | None = None) -> T:
    """Call `fn`; on `LLMUnavailable` wait and retry once per delay (default `RETRY_DELAYS`). Other exceptions
    propagate at once."""
    for delay in RETRY_DELAYS if delays is None else delays:
        try:
            return await fn()
        except LLMUnavailable:
            await asyncio.sleep(delay)
    return await fn()
