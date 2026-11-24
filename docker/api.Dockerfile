# Roadmap tutor platform API + built web app, for the local stack only. The HF Space is a Gradio SDK Space
# and uses neither this file nor the root Dockerfile.
# Build from the repo root: docker build -f docker/api.Dockerfile .

FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# CPU-only PyTorch first (same index for amd64 and arm64) so sentence-transformers doesn't pull CUDA wheels
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt requirements-platform.txt ./
RUN pip install -r requirements-platform.txt

COPY backend ./backend
COPY --from=web /web/dist ./web/dist

# Code stays root-owned and read-only for the runtime user
RUN useradd --create-home --uid 10001 tutor
USER tutor

# Secrets arrive only through the container environment (compose env_file); never read backend/.env here.
# `uv` (requirements-platform.txt) provides the `uvx` the stdio MCP servers run with; it downloads each pinned
# package on first use into the runtime user's cache. The gateway starts stdio servers with a minimal environment
# (HOME, PATH, ...), where uv's default cache is the same $HOME/.cache/uv.
ENV SKIP_DOTENV=1 HOST=0.0.0.0 PORT=8000 WEB_DIST=/app/web/dist UV_CACHE_DIR=/home/tutor/.cache/uv
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8000')}/api/health\", timeout=4)"]
CMD ["python", "-m", "backend.tutor"]
