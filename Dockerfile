# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.14
ARG POETRY_VERSION=2.4.1

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ARG POETRY_VERSION
ENV POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR=/tmp/poetry-cache

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN --mount=type=cache,target=/tmp/poetry-cache \
    poetry install --only main --no-root --no-ansi

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG APP_UID=10001
ARG APP_GID=10001
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src"

RUN groupadd --gid ${APP_GID} appuser \
    && useradd --uid ${APP_UID} --gid ${APP_GID} \
        --no-create-home --shell /bin/false appuser

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY src/ ./src/
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

EXPOSE 8001
USER appuser

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "--factory", "app.entrypoints.http.main:create_app", "--host", "0.0.0.0", "--port", "8001"]
