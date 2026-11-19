"""SKIP_DOTENV=1 stops backend/.env from overriding the process environment (used by the E2E servers)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Records load_dotenv calls instead of reading the real backend/.env, then imports config fresh.
_PROBE = """
import json, dotenv
calls = []
dotenv.load_dotenv = lambda *a, **k: calls.append({"path": str(a[0]) if a else None, **k})
from backend.core import config
print(json.dumps({"calls": calls, "passcode_matches": config.PREMIUM_PASSCODE == "sentinel-from-env"}))
"""


def _probe(**extra_env: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "SKIP_DOTENV"}
    env.update(PREMIUM_PASSCODE="sentinel-from-env", **extra_env)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=REPO, env=env, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_skip_dotenv_keeps_process_env():
    result = _probe(SKIP_DOTENV="1")
    assert result["calls"] == []
    assert result["passcode_matches"] is True


def test_dotenv_still_overrides_by_default():
    result = _probe()
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["override"] is True
    assert Path(call["path"]) == REPO / "backend" / ".env"
