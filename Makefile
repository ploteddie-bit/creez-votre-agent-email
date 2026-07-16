# Agent Mail 24/7 - Makefile
# Cible : install, test, run, dev, dashboard, health, setup-oauth, clean

PYTHON ?= python3
VENV ?= venv
PIP ?= $(VENV)/bin/pip
PY := $(VENV)/bin/python

# Couleurs pour l'output
GREEN := \033[0;32m
YELLOW := \033[1;33m
RED := \033[0;31m
NC := \033[0m # No Color

.PHONY: help install bootstrap test test-fast test-contracts run dashboard health sync embed process setup-oauth clean lint format

help:  ## Affiche cette aide
	@echo "$(GREEN)Agent Mail 24/7 - Commandes disponibles :$(NC)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-20s$(NC) %s\n", $$1, $$2}'

install:  ## Installe les dependances dans un venv
	@if [ ! -d "$(VENV)" ]; then \
		echo "$(YELLOW)Creating venv...$(NC)"; \
		$(PYTHON) -m venv $(VENV); \
	fi
	@echo "$(YELLOW)Installing dependencies...$(NC)"
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "$(GREEN)OK$(NC)"

bootstrap:  ## Premier lancement : cree .env et config.yaml depuis les examples
	@echo "$(YELLOW)Bootstrapping configuration...$(NC)"
	@if [ ! -f configs/.env ]; then \
		cp configs/.env.example configs/.env; \
		echo "$(GREEN)Created configs/.env$(NC)"; \
		echo "$(RED)IMPORTANT: Editer configs/.env et mettre les vraies valeurs$(NC)"; \
	else \
		echo "$(YELLOW)configs/.env existe deja, skip$(NC)"; \
	fi
	@if [ ! -f configs/config.yaml ]; then \
		cp configs/config.yaml.example configs/config.yaml; \
		echo "$(GREEN)Created configs/config.yaml$(NC)"; \
		echo "$(RED)IMPORTANT: Editer configs/config.yaml et mettre les vraies IPs$(NC)"; \
	else \
		echo "$(YELLOW)configs/config.yaml existe deja, skip$(NC)"; \
	fi
	@echo "$(GREEN)Bootstrap OK. Voir docs/SETUP-OAUTH.md pour configurer Gmail.$(NC)"

test:  ## Lance tous les tests
	@echo "$(YELLOW)Running tests...$(NC)"
	$(PY) -m pytest tests/ -v

test-fast:  ## Tests rapides (skip les lents)
	$(PY) -m pytest tests/ -v -m "not slow"

test-contracts:  ## Tests de contrat SQL <-> schema
	$(PY) -m pytest tests/test_sql_contracts.py -v

test-coverage:  ## Tests avec couverture
	$(PY) -m pytest tests/ --cov=src --cov-report=term-missing

run:  ## Lance le daemon en boucle infinie
	@echo "$(GREEN)Starting daemon...$(NC)"
	$(PY) -m src.main

dev:  ## Lance le daemon en mode debug
	$(PY) -m src.main --debug

dashboard:  ## Lance le dashboard FastAPI (port 8000 par defaut)
	$(PY) -m src.main dashboard --debug

health:  ## Affiche l'etat de sante
	$(PY) -m src.main health

sync:  ## Sync Gmail une fois (delta)
	$(PY) -m src.main sync

sync-full:  ## Sync Gmail full (6 mois)
	$(PY) -m src.main sync --full --max 2000

embed:  ## Embedde 100 mails non traites
	$(PY) -m src.main embed 100

process:  ## Process 50 mails (P0 -> P1)
	$(PY) -m src.main process 50

setup-oauth:  ## Guide interactif OAuth Gmail
	$(PY) -m src.main setup-oauth

migrate:  ## Applique les migrations alembic
	$(PY) -m alembic upgrade head

migrate-down:  ## Rollback d'une migration
	$(PY) -m alembic downgrade -1

lint:  ## Verifie le style avec ruff
	$(PY) -m ruff check src/ tests/

format:  ## Formate le code avec black
	$(PY) -m black src/ tests/

clean:  ## Nettoie les artefacts (cache Python, build, .pyc)
	@echo "$(YELLOW)Cleaning...$(NC)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "$(GREEN)Clean OK$(NC)"

clean-all: clean  ## Nettoie aussi le venv
	rm -rf $(VENV)
	@echo "$(GREEN)Full clean OK$(NC)"
