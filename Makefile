PYTHON ?= python
COMPOSE ?= docker compose

-include .env

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
	$(COMPOSE) up -d --build

up: compose

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

db-reset:
	$(COMPOSE) down -v
	$(COMPOSE) up -d --build postgres

migrate:
	$(COMPOSE) run --rm api python src/data/apply_migrations.py --database-url "$(DOCKER_DATABASE_URL)"

api:
	$(COMPOSE) up --build api

test:
	$(COMPOSE) run --rm api python -m pytest src/transcription-api/tests
