"""Per-turn budgets and the retry policy for model calls."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from backend.core.llm import LLMUnavailable
from backend.core.trace import Trace

T = TypeVar("T")

OUTAGE_MESSAGE = "The model is unavailable right now. Your session is saved; try again in a minute."


class BudgetExceeded(Exception):
    def __init__(self, kind: str, used: str, limit: str):
        self.message = (f"This turn reached its {kind} budget ({used} of {limit}). "
                        "Your session is saved; ask again to continue.")
        super().__init__(self.message)


@dataclass(frozen=True)
class Budget:
    """`max_steps` is carried for callers: `run_tool_loop` enforces it, so `check` never raises for steps."""

    max_steps: int
    max_tokens: int
    max_usd: float

    def check(self, trace: Trace) -> None:
        """Raise `BudgetExceeded` when the trace's tokens or dollars are strictly over their limit."""
        s = trace.summary()
        tokens = s["input_tokens"] + s["output_tokens"] + s["cache_read_tokens"] + s["cache_write_tokens"]
        if tokens > self.max_tokens:
            raise BudgetExceeded("token", str(tokens), str(self.max_tokens))
        if s["cost_usd"] > self.max_usd:
            raise BudgetExceeded("cost", f"${s['cost_usd']:.4f}", f"${self.max_usd:.4f}")


async def with_llm_retry(fn: Callable[[], Awaitable[T]], *, delays: tuple[float, ...] = (1.0, 2.0)) -> T:
    """Call `fn`; on `LLMUnavailable` wait and retry once per delay. Other exceptions propagate at once."""
    for delay in delays:
        try:
            return await fn()
        except LLMUnavailable:
            await asyncio.sleep(delay)
    return await fn()
