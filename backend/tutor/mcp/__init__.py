"""MCP gateway pieces for the tutor platform."""

# The own servers (the registry's inproc targets), served under /mcp/<name>/ and by
# `python -m backend.tutor mcp <name>`.
OWN_SERVERS = ("knowledge", "learner", "ingestion", "web")
