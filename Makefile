## CCTV employee movement tracking - common operations.
## Everything runs in Docker; nothing needs to be installed on the host
## except Docker itself.

SHELL := /bin/sh
COMPOSE := docker compose
GPU := docker compose -f docker-compose.yml -f docker-compose.gpu.yml

.DEFAULT_GOAL := help
.PHONY: help init up down restart logs ps build rebuild migrate makemigrations \
        seed demo shell dbshell superuser test lint fmt mock mock-stop \
        vision vision-gpu audit benchmark loadtest health reset lock

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ----------------------------------------------------------------- bootstrap

init: ## First-time setup: create .env with generated keys, then build
	@if [ -f .env ]; then \
	  echo ".env already exists; leaving it alone"; \
	else \
	  cp .env.example .env; \
	  echo "generating keys into .env ..."; \
	  SECRET=$$(docker run --rm python:3.12-slim python -c "import secrets;print(secrets.token_urlsafe(64))"); \
	  FERNET=$$(docker run --rm python:3.12-slim sh -c "pip -q install cryptography >/dev/null 2>&1 && python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'"); \
	  PGPASS=$$(docker run --rm python:3.12-slim python -c "import secrets;print(secrets.token_urlsafe(24))"); \
	  MINIOPASS=$$(docker run --rm python:3.12-slim python -c "import secrets;print(secrets.token_urlsafe(24))"); \
	  ADMINPASS=$$(docker run --rm python:3.12-slim python -c "import secrets;print(secrets.token_urlsafe(16))"); \
	  sed -i.bak "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$$SECRET|" .env; \
	  sed -i.bak "s|^FIELD_ENCRYPTION_KEY=.*|FIELD_ENCRYPTION_KEY=$$FERNET|" .env; \
	  sed -i.bak "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$$PGPASS|" .env; \
	  sed -i.bak "s|^MINIO_ROOT_PASSWORD=.*|MINIO_ROOT_PASSWORD=$$MINIOPASS|" .env; \
	  sed -i.bak "s|^BOOTSTRAP_ADMIN_PASSWORD=.*|BOOTSTRAP_ADMIN_PASSWORD=$$ADMINPASS|" .env; \
	  rm -f .env.bak; \
	  echo ""; \
	  echo "  .env created. Your first admin password is:"; \
	  grep '^BOOTSTRAP_ADMIN_PASSWORD=' .env; \
	  echo "  Change it after first sign-in, then delete that line."; \
	  echo ""; \
	fi
	@$(MAKE) build

build: ## Build the application images
	$(COMPOSE) build web

rebuild: ## Rebuild images from scratch, no cache
	$(COMPOSE) build --no-cache web

# ---------------------------------------------------------------- lifecycle

up: ## Start the stack (db, redis, minio, web, worker, beat, aggregator)
	$(COMPOSE) up -d
	@echo ""
	@echo "  app       http://localhost:$${WEB_PORT:-8000}"
	@echo "  admin     http://localhost:$${WEB_PORT:-8000}/admin/"
	@echo "  api docs  http://localhost:$${WEB_PORT:-8000}/api/v1/docs/"
	@echo "  minio     http://localhost:$${MINIO_CONSOLE_PORT:-9001}"
	@echo ""
	@echo "  Next: make demo   (seeds an office and one user per role)"

down: ## Stop the stack, keep the data
	$(COMPOSE) down

restart: ## Restart the application containers
	$(COMPOSE) restart web worker beat aggregator

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail logs for all services (make logs s=aggregator for one)
	$(COMPOSE) logs -f --tail=100 $(s)

health: ## Check readiness of the web container
	@curl -fsS http://localhost:$${WEB_PORT:-8000}/readyz/ && echo "" || echo "not ready"

# ------------------------------------------------------------------ database

migrate: ## Apply migrations
	$(COMPOSE) run --rm web python manage.py migrate

makemigrations: ## Generate migrations for model changes
	$(COMPOSE) run --rm web python manage.py makemigrations

seed: ## Seed role groups and admin permissions
	$(COMPOSE) run --rm web python manage.py seed_roles

demo: ## Seed a demo office, cameras, employees and one user per role
	$(COMPOSE) run --rm web python manage.py seed_demo

superuser: ## Create an administrator interactively
	$(COMPOSE) run --rm web python manage.py createsuperuser

shell: ## Django shell
	$(COMPOSE) run --rm web python manage.py shell

dbshell: ## psql shell
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-cctv} -d $${POSTGRES_DB:-cctv}

reset: ## DESTRUCTIVE. Delete all containers and volumes, including the database
	@printf "This deletes the database, snapshots and models. Type 'yes' to confirm: "; \
	read ans; [ "$$ans" = "yes" ] || { echo "aborted"; exit 1; }
	$(COMPOSE) down -v

# -------------------------------------------------------------- vision tier

mock: ## Run the realtime path with synthetic movement (no cameras, no GPU)
	MOCK_VISION=1 $(COMPOSE) --profile vision up -d --build vision
	@echo ""
	@echo "  Synthetic movement is now flowing. Watch it arrive:"
	@echo "    make logs s=aggregator"
	@echo "  Then open the live board: http://localhost:$${WEB_PORT:-8000}/live/"

mock-stop: ## Stop the mock generator
	$(COMPOSE) --profile vision stop vision

vision: ## Start the vision workers against real cameras, CPU
	$(COMPOSE) --profile vision up -d --build vision

vision-gpu: ## Start the vision workers with CUDA
	$(GPU) --profile vision up -d --build vision
	@echo "verifying the GPU is visible inside the container:"
	@$(GPU) exec vision python -c "import onnxruntime as o; print(o.get_available_providers())"

audit: ## Camera viability audit. Run this before anything else. make audit secs=120
	$(COMPOSE) --profile vision run --rm vision \
	  python scripts/camera_audit.py --seconds $${secs:-60} --save

benchmark: ## Accuracy against a labelled set. make benchmark data=/samples/benchmark
	$(COMPOSE) --profile vision run --rm vision 	  python scripts/benchmark_accuracy.py --data $${data:-/samples/benchmark} --json /samples/benchmark-result.json

loadtest: ## Aggregator throughput. make loadtest cams=20 fps=4 secs=60 (never on production)
	$(COMPOSE) --profile vision run --rm vision 	  python scripts/load_test.py --cameras $${cams:-10} --fps $${fps:-4} --seconds $${secs:-30} --not-production
	$(COMPOSE) exec web python manage.py purge_load_test

# ------------------------------------------------------------------- quality

test: ## Run the test suite
	$(COMPOSE) run --rm -e DJANGO_DEBUG=0 web pytest -q

lint: ## Lint with ruff and run Django system checks
	$(COMPOSE) run --rm web sh -c "ruff check . && python manage.py check --deploy"

fmt: ## Format with ruff
	$(COMPOSE) run --rm web ruff format .

lock: ## Freeze dependency versions into requirements/*.lock
	$(COMPOSE) run --rm web sh -c \
	  "pip install pip-tools && pip-compile requirements/base.txt -o requirements/base.lock"
