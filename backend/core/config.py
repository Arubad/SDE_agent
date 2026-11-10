"""Central configuration. Every tunable lives here and can be overridden by an env var."""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
# .env wins over the shell locally (stale placeholders like GEMINI_API_KEY="..." are common);
# on HF Spaces there is no .env file, so Space secrets (env vars) are used as-is.
load_dotenv(BACKEND_DIR / ".env", override=True)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# --- LLMs -------------------------------------------------------------------
# Claude Haiku 5.5 on Microsoft Foundry: router, generator, verifier, eval judge
FOUNDRY_API_KEY = _env("TESTFORGE_FOUNDRY_API_KEY") or _env("ANTHROPIC_FOUNDRY_API_KEY")
FOUNDRY_ENDPOINT = _env("TESTFORGE_FOUNDRY_ENDPOINT") or _env("ANTHROPIC_FOUNDRY_BASE_URL")
HAIKU_MODEL = _env("FOUNDRY_HAIKU_DEPLOYMENT", "claude-haiku-5-5")

# Gemini: native YouTube video understanding (and optional alternate generator)
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-3.5-flash")

# Which model writes the final answer: "haiku" (default) or "gemini"
GENERATOR = _env("GENERATOR", "haiku").lower()

# --- Retrieval ----------------------------------------------------------------
QDRANT_URL = _env("QDRANT_URL")
QDRANT_API_KEY = _env("QDRANT_API_KEY")
QDRANT_LOCAL_PATH = _env("QDRANT_LOCAL_PATH", str(BACKEND_DIR / "qdrant_local"))
KB_COLLECTION = _env("KB_COLLECTION", "sde_kb_v2")
CACHE_COLLECTION = _env("CACHE_COLLECTION", "sde_answer_cache")

DENSE_MODEL = "BAAI/bge-small-en-v1.5"
DENSE_DIM = 384
SPARSE_MODEL = "Qdrant/bm25"
# BGE retrieval works best with this instruction prepended to queries (not to passages)
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

RETRIEVAL_MODE = _env("RETRIEVAL_MODE", "hybrid")  # dense | sparse | hybrid | hybrid_rerank (eval: rerank not significant, 7x slower)
RERANKER_MODEL = _env("RERANKER_MODEL", "BAAI/bge-reranker-base")
CANDIDATES_K = int(_env("CANDIDATES_K", "15"))  # fetched before reranking
TOP_K = int(_env("TOP_K", "5"))

# --- Access tiers ---------------------------------------------------------------
# Public visitors get the free Gemini pipeline. The passcode unlocks the Claude Haiku pipeline
# (paid Foundry deployment) and the authenticated MCP endpoint. Unset = premium tier disabled.
PREMIUM_PASSCODE = _env("PREMIUM_PASSCODE")
PASSCODE_MAX_FAILURES = int(_env("PASSCODE_MAX_FAILURES", "5"))  # per IP, per 15 minutes

# --- Agent limits ---------------------------------------------------------------
MAX_LLM_CALLS = int(_env("MAX_LLM_CALLS", "6"))
REQUEST_TIMEOUT_S = float(_env("REQUEST_TIMEOUT_S", "150"))
TOOL_TIMEOUT_S = float(_env("TOOL_TIMEOUT_S", "45"))
VIDEO_TOOL_TIMEOUT_S = float(_env("VIDEO_TOOL_TIMEOUT_S", "150"))
MAX_CONTEXT_CHUNKS = int(_env("MAX_CONTEXT_CHUNKS", "8"))
HISTORY_TURNS = int(_env("HISTORY_TURNS", "6"))
VERIFY_ANSWERS = _env("VERIFY_ANSWERS", "true").lower() == "true"
SEMANTIC_CACHE = _env("SEMANTIC_CACHE", "true").lower() == "true"
CACHE_THRESHOLD = float(_env("CACHE_THRESHOLD", "0.95"))
CACHE_TTL_S = int(_env("CACHE_TTL_S", str(24 * 3600)))
