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
# server. swap/ is copied BEFORE `pip install .` so the package is present at build.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY swap ./swap
RUN pip install --upgrade pip && pip install .

# stdio transport: Glama spawns the container and speaks MCP over stdin/stdout.
CMD ["python", "-m", "swap.mcp_app"]
