"""Deploy isolation: the HF Space must never receive the local platform code."""
import fnmatch
import getpass
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_import_has_no_side_effects(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("getpass called on import")

    monkeypatch.setattr(getpass, "getpass", boom)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    sys.modules.pop("deploy_to_hf", None)
    mod = importlib.import_module("deploy_to_hf")
    assert callable(mod.main)


try:  # the matcher upload_folder itself uses; fall back to fnmatch if huggingface_hub is absent
    from huggingface_hub.utils import filter_repo_objects as _hf_filter
except ImportError:  # pragma: no cover - depends on the installed packages
    _hf_filter = None

IGNORED = [
    "backend/tutor/x.py",
    "web/src/App.tsx",
    "docker/api.Dockerfile",
    "compose.yaml",
    "compose.tunnel.yaml",
    "requirements-platform.txt",
    ".superpowers/sdd/2026-11-19-roadmap-tutor-phase1-foundation/global-constraints.md",
    ".claude/settings.local.json",
    "docs/superpowers/plans/plan.md",
    "docs/platform/running-locally.md",
    "backend/eval/recordings/case-01.json",
]
SHIPPED = [
    "app.py",
    "backend/server.py",
    "backend/core/llm.py",
    "frontend/index.html",
    "requirements.txt",
]


def _ignored(path: str) -> bool:
    import deploy_to_hf

    if _hf_filter is not None:
        return not list(_hf_filter([path], ignore_patterns=deploy_to_hf.IGNORE_PATTERNS))
    return any(fnmatch.fnmatch(path, p) for p in deploy_to_hf.IGNORE_PATTERNS)


def test_platform_paths_are_ignored():
    for path in IGNORED:
        assert _ignored(path), path


def test_space_runtime_files_are_not_ignored():
    for path in SHIPPED:
        assert not _ignored(path), path


def test_fnmatch_agrees_with_hf_matcher():
    import deploy_to_hf

    for path in IGNORED + SHIPPED:
        assert any(fnmatch.fnmatch(path, p) for p in deploy_to_hf.IGNORE_PATTERNS) == _ignored(path), path


def test_runtime_requirements_have_no_platform_deps():
    text = (ROOT / "requirements.txt").read_text().lower()
    for dep in ["sqlalchemy", "alembic", "psycopg", "langgraph", "opentelemetry"]:
        assert dep not in text, dep
