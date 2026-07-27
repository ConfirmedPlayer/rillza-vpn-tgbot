ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.8

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /app

# Dependencies first: this layer is cached until the lockfile changes.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock alembic.ini docker-entrypoint.sh ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

RUN chmod +x docker-entrypoint.sh \
    && useradd --create-home --uid 1000 rillza \
    && chown -R rillza:rillza /app
USER rillza

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "app"]
