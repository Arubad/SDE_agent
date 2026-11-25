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
         "enabled", "untrusted", "timeout_s", "tool_timeouts", "strip_prefix", "note"}
DEFAULT_URL_SOURCES = ("web.search",)


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
    tool_timeouts: dict[str, float] = field(default_factory=dict)  # by the server's own tool name
    strip_prefix: str | None = None
    note: str = ""

    def timeout_for(self, tool_name: str) -> float:
        """The call budget for one tool, by the name the server gives it (before `strip_prefix`)."""
        return self.tool_timeouts.get(tool_name, self.timeout_s)


@dataclass
class Registry:
    servers: tuple[ServerSpec, ...]
    agents: dict[str, tuple[str, ...]]
    deny: tuple[str, ...] = ()  # canonical-name globs no agent may call
    ask: tuple[str, ...] = ()  # canonical-name globs that need the caller's approval each call
    url_sources: tuple[str, ...] = DEFAULT_URL_SOURCES  # canonical tools whose result URLs become allowed sources


def _expand(value: str, env: Mapping[str, str]) -> str:
    def sub(m: re.Match) -> str:
        got = env.get(m.group(1))
        if got:
            return got
        return m.group(2) if m.group(2) is not None else ""

    return _ENV.sub(sub, value)


def _positive(t: object) -> bool:
    return not isinstance(t, bool) and isinstance(t, int | float) and t > 0


def _check_types(raw: dict) -> None:
    """JSON types, strictly: a string "false" is not a boolean, a string "5" is not a timeout."""
    name = raw.get("name")
    for key in ("enabled", "untrusted"):
        if key in raw and not isinstance(raw[key], bool):
            raise RegistryError(f"server {name!r}: {key} must be true or false (got {raw[key]!r})")
    if "timeout_s" in raw and not _positive(raw["timeout_s"]):
        raise RegistryError(f"server {name!r}: timeout_s must be a number > 0 (got {raw['timeout_s']!r})")
    if "tool_timeouts" in raw:
        tt = raw["tool_timeouts"]
        if not (isinstance(tt, dict) and all(_positive(v) for v in tt.values())):
            raise RegistryError(f"server {name!r}: tool_timeouts must be an object of numbers > 0 (got {tt!r})")
    for key in ("args", "requires_env"):
        if key in raw and not (isinstance(raw[key], list) and all(isinstance(a, str) for a in raw[key])):
            raise RegistryError(f"server {name!r}: {key} must be a list of strings")  # no value: args may hold one
    if "headers" in raw:
        h = raw["headers"]
        if not (isinstance(h, dict) and all(isinstance(v, str) for v in h.values())):
            raise RegistryError(f"server {name!r}: headers must be an object of strings")
    for key in ("name", "namespace", "transport", "target", "command", "url", "strip_prefix", "note"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise RegistryError(f"server {name!r}: {key} must be a string (got {raw[key]!r})")


def _check_secret_vars(raw: dict) -> None:
    """Every `${VAR}` without a default in `url`, `headers` or `args` is a secret, so it must be in `requires_env`:
    health and errors redact exactly the `requires_env` values (and the header values)."""
    places = [raw.get("url") or "", *(raw.get("headers") or {}).values(), *raw.get("args", ())]
    needed = {m.group(1) for v in places for m in _ENV.finditer(v) if m.group(2) is None}
    missing = sorted(needed - set(raw.get("requires_env", ())))
    if missing:
        raise RegistryError(f"server {raw['name']!r}: {', '.join('${' + v + '}' for v in missing)} has no default, "
                            "so it must be listed in requires_env")


def _spec(raw: object, env: Mapping[str, str]) -> ServerSpec:
    if not isinstance(raw, dict):
        raise RegistryError(f"server entry must be an object (got {raw!r})")
    unknown = set(raw) - _KEYS
    if unknown:
        raise RegistryError(f"unknown keys in server entry {raw.get('name')!r}: {sorted(unknown)}")
    for key in ("name", "namespace", "transport"):
        if not raw.get(key):
            raise RegistryError(f"server entry is missing {key!r}: {raw!r}")
    _check_types(raw)
    if raw["transport"] not in TRANSPORTS:
        raise RegistryError(f"server {raw['name']!r}: unknown transport {raw['transport']!r}")
    _check_secret_vars(raw)
    args = tuple(_expand(a, env) for a in raw.get("args", ()))
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
        headers={k: _expand(v, env) for k, v in (raw.get("headers") or {}).items()},
        requires_env=tuple(raw.get("requires_env", ())),
        enabled_default=raw.get("enabled", True),
        untrusted=raw.get("untrusted", True),
        tool_timeouts={k: float(v) for k, v in (raw.get("tool_timeouts") or {}).items()},
        strip_prefix=raw.get("strip_prefix"), note=raw.get("note", ""), **extra,
    )


def load_registry(path: Path, env: Mapping[str, str], *, allow_unknown: bool = False) -> Registry:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RegistryError(f"cannot read registry {path}: {e}") from e
    if not isinstance(data, dict) or set(data) - {"servers", "agents", "rules", "url_sources"}:
        raise RegistryError("registry must be an object with only 'servers', 'agents', 'rules' and 'url_sources'")
    servers = tuple(_spec(raw, env) for raw in data.get("servers", []))
    for attr in ("name", "namespace"):
        seen: set[str] = set()
        for s in servers:
            if getattr(s, attr) in seen:
                raise RegistryError(f"duplicate server {attr} {getattr(s, attr)!r}")
            seen.add(getattr(s, attr))
    agents = {a: tuple(pats) for a, pats in (data.get("agents") or {}).items()}
    rules = _rules(data.get("rules", {}))
    sources = _url_sources(data.get("url_sources", list(DEFAULT_URL_SOURCES)))
    if not allow_unknown:
        namespaces = {s.namespace for s in servers}
        owners = [(f"agent {a!r}", pats) for a, pats in agents.items()]
        owners += [(f"rules.{k}", v) for k, v in rules.items()]
        if "url_sources" in data:  # the default (web.search) is not required to exist
            owners.append(("url_sources", sources))
        for owner, pats in owners:
            for pat in pats:
                if not any(_could_match(pat, ns) for ns in namespaces):
                    raise RegistryError(f"{owner}: pattern {pat!r} matches no server namespace")
    return Registry(servers=servers, agents=agents, url_sources=sources, **rules)


def _url_sources(raw: object) -> tuple[str, ...]:
    """Canonical tool names (not globs) whose result URLs become allowed sources; default `web.search`."""
    if not isinstance(raw, list) or not all(isinstance(n, str) and n for n in raw):
        raise RegistryError("url_sources must be a list of canonical tool names")
    return tuple(raw)


def _could_match(pattern: str, namespace: str) -> bool:
    """Whether `pattern` could match some canonical `<namespace>.<tool>`, dotted namespaces included: it matches a
    probe name, or its literal head (before the first `*?[`) and `namespace.` agree as far as both go."""
    head = namespace + "."
    lit = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    return fnmatchcase(head + "x", pattern) or head.startswith(lit) or lit.startswith(head)


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
