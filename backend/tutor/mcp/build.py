"""Build the platform's own MCP servers from the API's dependencies (engine, net, index, embedder)."""
from __future__ import annotations

import os

from fastmcp import FastMCP

from backend.tutor.mcp import OWN_SERVERS
from backend.tutor.mcp.gateway import Gateway
from backend.tutor.mcp.registry import load_registry
from backend.tutor.mcp.servers.ingestion import build_ingestion_server
from backend.tutor.mcp.servers.knowledge import build_knowledge_server
from backend.tutor.mcp.servers.learner import build_learner_server
from backend.tutor.mcp.servers.web import build_web_server


def build_own_server(name: str, engine, net, index, embedder, settings, on_settled) -> FastMCP:
    if name == "knowledge":
        return build_knowledge_server(engine, index)
    if name == "learner":
        return build_learner_server(engine, embedder)
    if name == "ingestion":
        return build_ingestion_server(engine, net, settings, on_settled=on_settled)
    if name == "web":
        return build_web_server(net)
    raise ValueError(f"no own MCP server {name!r} (expected one of {', '.join(OWN_SERVERS)})")


def build_own_servers(engine, net, index, embedder, settings, on_settled) -> dict[str, FastMCP]:
    """Keyed `knowledge`, `learner`, `ingestion`, `web` (the registry's inproc targets)."""
    return {name: build_own_server(name, engine, net, index, embedder, settings, on_settled)
            for name in OWN_SERVERS}


def build_gateway(engine, net, index, embedder, settings, on_settled) -> Gateway:
    """The gateway the API (and the CLI's ingest) runs: the registry at `settings.mcp_registry` expanded with the
    process env, and an inproc set of own servers built for it alone (a FastMCP instance is never shared between
    event loops). Not started: call `start()`, and `stop()` when done."""
    registry = load_registry(settings.mcp_registry, os.environ)
    return Gateway(registry, engine=engine, inproc=build_own_servers(engine, net, index, embedder, settings, on_settled),
                   env=os.environ)
