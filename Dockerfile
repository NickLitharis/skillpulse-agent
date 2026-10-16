# SkillPulse runtime image (M6).
# One image for every workload; the Helm chart picks the command:
#   api        -> uvicorn skillpulse.api.app:app
#   mcp        -> skillpulse-mcp --transport streamable-http
#   worker     -> celery -A skillpulse.workers.app worker -Q fetch|skills
#   beat       -> celery -A skillpulse.workers.app beat
#   migrate    -> alembic upgrade head  (Helm pre-install/pre-upgrade hook Job)
#
# The install is editable, so /app/src is the package and
# src/skillpulse/extraction/taxonomy.py resolves REPO_ROOT as /app — config/skills.yaml
# and alembic/ must keep their repository layout inside the image.

FROM ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: re-sync only when the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --gid 10001 skillpulse \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin skillpulse

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SKILLPULSE_LOG_LEVEL=INFO

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "skillpulse.api.app:app", "--host", "0.0.0.0", "--port", "8000"]