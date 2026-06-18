PYTHON ?= python
COMPOSE ?= docker compose

-include .env

WHISPER_DEVICE ?= cpu
COMPOSE_FILES := -f docker-compose.yml
ifneq ($(filter cuda,$(WHISPER_DEVICE)),)
COMPOSE_FILES += -f docker-compose.gpu.yml
endif
COMPOSE_CMD = $(COMPOSE) $(COMPOSE_FILES)

POSTGRES_DB ?= discord_anthropologist
POSTGRES_USER ?= discord
POSTGRES_PASSWORD ?= discord
POSTGRES_HOST ?= 127.0.0.1
POSTGRES_PORT ?= 5432
DATABASE_URL ?= postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)
DOCKER_DATABASE_URL ?= postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@postgres:5432/$(POSTGRES_DB)

.PHONY: help compose up down logs db-reset migrate api test

help:
	@echo "make compose   Build and start API + Postgres"
	@echo "make up        Alias for compose"
	@echo "make down      Stop containers"
	@echo "make logs      Follow container logs"
	@echo "make db-reset  Recreate local Docker volumes"
	@echo "make migrate   Apply migrations in Docker"
	@echo "make api       Run API in Docker foreground"
	@echo "make test      Run tests in Docker"

compose:
	$(COMPOSE_CMD) up -d --build

up: compose

down:
	$(COMPOSE_CMD) down

logs:
	$(COMPOSE_CMD) logs -f

db-reset:
	$(COMPOSE_CMD) down -v
	$(COMPOSE_CMD) up -d --build postgres

migrate:
	$(COMPOSE_CMD) run --rm api python src/data/apply_migrations.py --database-url "$(DOCKER_DATABASE_URL)"

api:
	$(COMPOSE_CMD) up --build api

test:
	$(COMPOSE_CMD) build api
	$(COMPOSE_CMD) run --rm api env PYTHONPATH=/app/src:/app/src/transcription-api python -m pytest --import-mode=importlib src/transcription-api/tests
