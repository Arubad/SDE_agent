"""The MCP server registry: a committed JSON file, env expansion, validation and per-server on/off overrides."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.tutor.models import McpServerState

TRANSPORTS = ("inproc", "stdio", "http")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_KEYS = {"name", "namespace", "transport", "target", "command", "args", "url", "headers", "requires_env",
         "enabled", "untrusted", "timeout_s", "strip_prefix", "note"}


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ServerSpec:
    name: str
    namespace: str
    transport: Literal["inproc", "stdio", "http"]
    target: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    requires_env: tuple[str, ...] = ()
    enabled_default: bool = True
    untrusted: bool = True
    timeout_s: float = 20.0
    strip_prefix: str | None = None
    note: str = ""


@dataclass
class Registry:
    servers: tuple[ServerSpec, ...]
    agents: dict[str, tuple[str, ...]]
    deny: tuple[str, ...] = ()  # canonical-name globs no agent may call
    ask: tuple[str, ...] = ()  # canonical-name globs that need the caller's approval each call


def _expand(value: str, env: Mapping[str, str]) -> str:
    def sub(m: re.Match) -> str:
        got = env.get(m.group(1))
        if got:
            return got
        return m.group(2) if m.group(2) is not None else ""

    return _ENV.sub(sub, value)


def _spec(raw: object, env: Mapping[str, str]) -> ServerSpec:
    if not isinstance(raw, dict):
        raise RegistryError(f"server entry must be an object (got {raw!r})")
    unknown = set(raw) - _KEYS
    if unknown:
        raise RegistryError(f"unknown keys in server entry {raw.get('name')!r}: {sorted(unknown)}")
    for key in ("name", "namespace", "transport"):
        if not raw.get(key):
            raise RegistryError(f"server entry is missing {key!r}: {raw!r}")
    if raw["transport"] not in TRANSPORTS:
        raise RegistryError(f"server {raw['name']!r}: unknown transport {raw['transport']!r}")
    args = tuple(_expand(str(a), env) for a in raw.get("args", ()))
    command = _expand(raw["command"], env) if raw.get("command") else None
    if raw["transport"] == "stdio" and command == "uvx":
        pkg = next((a for a in args if not a.startswith("-")), None)
        if pkg is None or "==" not in pkg:
            raise RegistryError(f"server {raw['name']!r}: uvx package {pkg!r} must be pinned with ==")
    extra = {}
    if "timeout_s" in raw:
        extra["timeout_s"] = float(raw["timeout_s"])
    return ServerSpec(
        name=raw["name"], namespace=raw["namespace"], transport=raw["transport"],
        target=raw.get("target"), command=command, args=args,
        url=_expand(raw["url"], env) if raw.get("url") else None,
        headers={k: _expand(str(v), env) for k, v in (raw.get("headers") or {}).items()},
        requires_env=tuple(raw.get("requires_env", ())),
        enabled_default=bool(raw.get("enabled", True)),
        untrusted=bool(raw.get("untrusted", True)),
        strip_prefix=raw.get("strip_prefix"), note=raw.get("note", ""), **extra,
    )


def load_registry(path: Path, env: Mapping[str, str], *, allow_unknown: bool = False) -> Registry:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RegistryError(f"cannot read registry {path}: {e}") from e
    if not isinstance(data, dict) or set(data) - {"servers", "agents", "rules"}:
        raise RegistryError("registry must be an object with only 'servers', 'agents' and 'rules'")
    servers = tuple(_spec(raw, env) for raw in data.get("servers", []))
    for attr in ("name", "namespace"):
        seen: set[str] = set()
        for s in servers:
            if getattr(s, attr) in seen:
                raise RegistryError(f"duplicate server {attr} {getattr(s, attr)!r}")
            seen.add(getattr(s, attr))
    agents = {a: tuple(pats) for a, pats in (data.get("agents") or {}).items()}
    rules = _rules(data.get("rules", {}))
    if not allow_unknown:
        namespaces = {s.namespace for s in servers}
        owners = [(f"agent {a!r}", pats) for a, pats in agents.items()]
        for owner, pats in [*owners, *((f"rules.{k}", v) for k, v in rules.items())]:
            for pat in pats:
                if not any(fnmatchcase(ns, pat.split(".", 1)[0]) for ns in namespaces):
                    raise RegistryError(f"{owner}: pattern {pat!r} matches no server namespace")
    return Registry(servers=servers, agents=agents, **rules)


def _rules(raw: object) -> dict[str, tuple[str, ...]]:
    """`{"deny": [globs], "ask": [globs]}`, both optional."""
    if not isinstance(raw, dict) or set(raw) - {"deny", "ask"}:
        raise RegistryError("'rules' must be an object with only 'deny' and 'ask'")
    out = {}
    for key, pats in raw.items():
        if not isinstance(pats, list) or not all(isinstance(p, str) and p for p in pats):
            raise RegistryError(f"rules.{key} must be a list of tool-name patterns")
        out[key] = tuple(pats)
    return out


def missing_env(spec: ServerSpec, env: Mapping[str, str]) -> list[str]:
    return [v for v in spec.requires_env if not env.get(v)]


def effective_enabled(engine: Engine | None, spec: ServerSpec) -> bool:
    if engine is None:
        return spec.enabled_default
    with Session(engine) as s:
        row = s.execute(select(McpServerState.enabled).where(McpServerState.name == spec.name)).scalar_one_or_none()
    return spec.enabled_default if row is None else row


def set_enabled(engine: Engine, name: str, enabled: bool) -> None:
    stmt = insert(McpServerState).values(name=name, enabled=enabled)
    stmt = stmt.on_conflict_do_update(
        index_elements=[McpServerState.name], set_={"enabled": enabled, "updated_at": func.now()}
    )
    with Session(engine) as s, s.begin():
        s.execute(stmt)
