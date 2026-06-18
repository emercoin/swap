# Glama introspection target — a standalone stdio MCP server with NO external deps.
#
# Glama builds this in an isolated Firecracker microVM (ephemeral FS, no outbound
# network to our VPS) and connects over MCP to run tools/list for TDQS scoring. It
# only READS tool definitions; it never executes a tool, so the adapter / TronGrid /
# EMC node / DB are all irrelevant here. We launch the dependency-free stdio entry
# (`python -m swap.mcp_app`), NOT the full FastAPI HTTP stack + watcher.
#
# Production deploy on the VPS uses deploy/Dockerfile (uvicorn HTTP on :8002) — that
# one is unchanged; this root file exists purely so Glama gets a clean introspectable
# server. swap/ is copied BEFORE the install so the package is present at build.
#
# Base image bundles uv + Python 3.12 (uv installs deps far faster than pip).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml ./
COPY swap ./swap
RUN uv pip install --system .

# stdio transport: Glama spawns the container and speaks MCP over stdin/stdout.
CMD ["python", "-m", "swap.mcp_app"]
