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


def test_platform_paths_are_ignored():
    import deploy_to_hf

    for path in [
        "backend/tutor/x.py",
        "web/src/App.tsx",
        "docker/api.Dockerfile",
        "compose.yaml",
        "compose.tunnel.yaml",
        "requirements-platform.txt",
    ]:
        assert any(fnmatch.fnmatch(path, p) for p in deploy_to_hf.IGNORE_PATTERNS), path


def test_runtime_requirements_have_no_platform_deps():
    text = (ROOT / "requirements.txt").read_text().lower()
    for dep in ["sqlalchemy", "alembic", "psycopg", "langgraph", "opentelemetry"]:
        assert dep not in text, dep
