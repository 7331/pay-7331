# Build from the repository root: docker build -f backend/Dockerfile .  (docker-compose.yml does).
# One image for the api (uvicorn app.main:app), the watcher (python -m app.watcher), the cli and migrations.
# The image keeps the repository layout (/app/backend, /app/frontend/dist, /app/contracts/abi), so the pages and the
# factory ABI are found by the same relative paths as in a checkout.

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

# The frontend's Vite build; only its dist/ goes into the image. pnpm comes from corepack at the
# version package.json's packageManager pins.
FROM node:22.23.1-slim AS frontend_build
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
WORKDIR /frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store pnpm install --frozen-lockfile
COPY frontend/ ./
# a+rX: files keep the checkout's modes, and a 600 logo is unreadable to the `pay` user (it served empty bodies).
RUN pnpm build && chmod -R a+rX dist

# The dependencies' venv, built where a compiler is: ckzg (eth-account) and PyYAML (uvicorn[standard]) publish no
# cp315 wheels yet (PyPI, 2026-10-21), so uv builds them from source. Only the venv goes into the runtime image.
FROM python:3.15-slim AS python_dependencies_build
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app/backend
RUN --mount=type=bind,source=backend/pyproject.toml,target=/app/backend/pyproject.toml \
    --mount=type=bind,source=backend/uv.lock,target=/app/backend/uv.lock \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

FROM python:3.15-slim
ENV PYTHONUNBUFFERED=1 \
    TZ=UTC \
    PATH="/app/backend/.venv/bin:${PATH}"
WORKDIR /app/backend
COPY --from=python_dependencies_build /app/backend/.venv /app/backend/.venv
RUN --mount=type=bind,target=/repository \
    cp -r /repository/backend/. /app/backend/ && \
    mkdir -p /app/contracts/abi && \
    cp /repository/contracts/abi/DepositForwarderFactory.json /app/contracts/abi/ && \
    python -m compileall -q /app/backend/app /app/backend/migrations && \
    chmod -R a+rX /app
COPY --from=frontend_build /frontend/dist /app/frontend/dist
RUN useradd --system --uid 10001 --no-create-home pay
USER pay
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
