.PHONY: help install setup dev test test-unit test-integration lint build up down logs shell vault-store db-migrate clean

PYTHON := python3
COMPOSE := docker compose

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Local dev setup ───────────────────────────────────────────────────────────
install: ## Install Python deps + Playwright + spaCy + NLTK
	pip install -r requirements.txt
	playwright install chromium
	playwright install-deps chromium
	python -m spacy download en_core_web_sm
	python -c "import nltk; nltk.download('punkt'); nltk.download('stopwords')"
	@echo "✓ All dependencies installed"

setup: install ## Full first-time setup (install + .env + vault)
	@[ -f .env ] || (cp .env.example .env && echo "Created .env — fill in your values")
	@mkdir -p .vault && chmod 700 .vault
	@echo "✓ Setup complete. Edit .env then run: make dev"

# ── Running ───────────────────────────────────────────────────────────────────
dev: ## Start infra (DB + Redis) + API in dev mode
	$(COMPOSE) up -d db redis
	@sleep 3
	uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

up: ## Start all services via Docker Compose
	$(COMPOSE) up -d --build
	@echo "✓ All services up"
	@echo "  Dashboard : http://localhost"
	@echo "  API       : http://localhost/api"
	@echo "  Flower    : http://localhost/flower"

down: ## Stop all services
	$(COMPOSE) down

logs: ## Follow logs for all services
	$(COMPOSE) logs -f --tail=100

logs-bot: ## Follow API logs only
	$(COMPOSE) logs -f bot --tail=100

shell: ## Open shell in bot container
	$(COMPOSE) exec bot bash

# ── Pipeline ──────────────────────────────────────────────────────────────────
run: ## Run pipeline (dry run — no real submissions)
	$(PYTHON) scripts/run_pipeline.py --resume resume.pdf --prefs prefs.example.json

run-live: ## Run pipeline with REAL submissions (careful!)
	@echo "⚠  This will submit REAL applications. DRY_RUN must be false in .env"
	@read -p "Type 'yes' to confirm: " confirm; [ "$$confirm" = "yes" ] || exit 1
	$(PYTHON) scripts/run_pipeline.py --resume resume.pdf --prefs prefs.example.json --no-dry-run

# ── Workers ───────────────────────────────────────────────────────────────────
worker-discovery: ## Start discovery worker locally
	celery -A infra.celery_app worker -Q discovery -c 2 --loglevel=info

worker-generation: ## Start generation worker locally
	celery -A infra.celery_app worker -Q generation -c 4 --loglevel=info

worker-apply: ## Start apply worker locally
	celery -A infra.celery_app worker -Q apply -c 2 --loglevel=info

beat: ## Start Celery beat scheduler
	celery -A infra.celery_app beat --loglevel=info

flower-local: ## Start Flower monitoring UI
	celery -A infra.celery_app flower --port=5555

# ── Testing ───────────────────────────────────────────────────────────────────
test: test-unit ## Run all fast tests

test-unit: ## Run unit tests (no network/LLM)
	pytest tests/unit/ -v --tb=short

test-integration: ## Run integration tests (needs ANTHROPIC_API_KEY + running DB+Redis)
	pytest tests/integration/ -v --tb=short -s

test-ci: ## Run tests as CI would (strict)
	pytest tests/unit/ -v --tb=short --junit-xml=test-results/unit.xml

# ── Code quality ──────────────────────────────────────────────────────────────
lint: ## Lint with ruff
	ruff check . --select E,F,W,I --ignore E501

format: ## Auto-format with ruff
	ruff format .

typecheck: ## Type check key modules
	mypy models/schemas.py agents/base_agent.py agents/verifier/ats_scorer.py --ignore-missing-imports

# ── Database ──────────────────────────────────────────────────────────────────
db-migrate: ## Run Alembic migrations
	alembic upgrade head

db-rollback: ## Rollback last migration
	alembic downgrade -1

db-shell: ## Open psql shell
	$(COMPOSE) exec db psql -U user internship_bot

# ── Security ──────────────────────────────────────────────────────────────────
vault-store: ## Store portal credentials in encrypted vault
	@read -p "Portal name (daad/euraxess/handshake): " portal; \
	python -m security.vault store $$portal

vault-list: ## List stored portal credentials
	python -m security.vault list

vault-audit: ## Show last 20 audit log entries
	python -m security.vault audit

# ── Docker ────────────────────────────────────────────────────────────────────
build: ## Build Docker image
	$(COMPOSE) build

push: ## Push image to GHCR (needs docker login)
	$(COMPOSE) push

health: ## Check API health
	@curl -sf http://localhost:8000/health | python3 -m json.tool

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean: ## Remove temp files + screenshots
	find /tmp -name "internship_bot_*" -exec rm -rf {} + 2>/dev/null || true
	find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "✓ Cleaned"

clean-all: down clean ## Stop services + clean
	$(COMPOSE) down -v   # removes volumes too — WARNING: deletes DB data
	@echo "✓ All data removed"

# ── Monitoring ────────────────────────────────────────────────────────────────
grafana: ## Open Grafana in browser
	@echo "Grafana: http://localhost:3000 (admin / changeme)"
	@which open >/dev/null 2>&1 && open http://localhost:3000 || echo "Visit http://localhost:3000"

prometheus: ## Open Prometheus in browser
	@which open >/dev/null 2>&1 && open http://localhost:9090 || echo "Visit http://localhost:9090"

alerts: ## Show currently firing Prometheus alerts
	@curl -sf http://localhost:9090/api/v1/alerts | python3 -m json.tool | grep -A3 '"state":"firing"' || echo "No firing alerts"

metrics: ## Show raw /metrics output from API
	@curl -sf http://localhost:8000/metrics | grep -v "^#" | grep "^bot_" | sort

reload-prometheus: ## Hot-reload Prometheus config (no restart)
	@curl -X POST http://localhost:9090/-/reload && echo "Prometheus reloaded"

security: ## Run security check before deploy
	JWT_SECRET=$${JWT_SECRET:-dummy} VAULT_MASTER_PASSWORD=$${VAULT_MASTER_PASSWORD:-dummy} \
	python scripts/security_check.py
