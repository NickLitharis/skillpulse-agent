# SkillPulse Agent -- developer entry points.
# Targets that need Docker (integration) are separate from the fast unit targets.

.PHONY: help install lint format typecheck test test-integration check db-up db-down redis-up migrate threads fetch worker beat

help:
	@echo "install          sync the uv environment"
	@echo "lint             ruff check"
	@echo "format           ruff format"
	@echo "typecheck        mypy (strict)"
	@echo "test             unit tests only (no Docker)"
	@echo "test-integration Postgres + Redis testcontainers tests (needs Docker)"
	@echo "check            lint + typecheck + unit tests"
	@echo "db-up / db-down  start/stop local Postgres via docker compose"
	@echo "redis-up         start local Redis via docker compose"
	@echo "migrate          alembic upgrade head"
	@echo "threads          list hiring threads found on Hacker News"
	@echo "fetch            fetch one thread: make fetch THREAD=12345"
	@echo "worker           run a Celery worker (solo pool)"
	@echo "beat             run the Celery beat scheduler (6-hour thread refresh)"

install:
	uv sync

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests

typecheck:
	uv run mypy

test:
	uv run pytest -m "not integration"

test-integration:
	uv run pytest -m integration

check: lint typecheck test

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

redis-up:
	docker compose up -d redis

worker:
	uv run celery -A skillpulse.workers.app worker --pool=solo --loglevel=$(LOGLEVEL)

beat:
	uv run celery -A skillpulse.workers.app beat --loglevel=$(LOGLEVEL)

migrate:
	uv run alembic upgrade head

threads:
	uv run skillpulse threads

fetch:
	uv run skillpulse fetch $(THREAD)

LOGLEVEL ?= info

# Disposable local Kubernetes deployment. Requires Docker, k3d, kubectl, Helm,
# Terraform, openssl, curl, jq and Node/npx. These commands do not enable live HN/LLM.
.PHONY: up down secrets smoke
up:
	bash scripts/up.sh

down:
	bash scripts/down.sh

secrets:
	KUBECONFIG="$(CURDIR)/infra/.kubeconfig" bash scripts/secrets.sh

smoke:
	bash scripts/smoke.sh
