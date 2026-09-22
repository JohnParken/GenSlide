.PHONY: install test test-verbose test-unit test-service run clean sync help dev-agentscope stop-agentscope test-flow

UV ?= uv

help:
	@echo "GenSlide development commands (powered by uv):"
	@echo "  make install         - Create virtual environment and install dependencies via uv sync"
	@echo "  make sync            - Sync all dependencies including dev tools"
	@echo "  make test            - Run root tests using pytest via uv"
	@echo "  make test-verbose    - Run tests in verbose mode"
	@echo "  make test-unit       - Run tests using built-in unittest via uv"
	@echo "  make test-service    - Run the genslide-agentscope service test suite"
	@echo "  make run             - Run the Streamlit workbench (frontend/service_chat.py)"
	@echo "  make clean           - Remove cached files and virtual environment"
	@echo "  make dev-agentscope  - Start the full local AgentScope dev stack"
	@echo "  make stop-agentscope - Stop the local AgentScope dev stack"
	@echo "  make test-flow       - Run the end-to-end AgentScope flow check"

install:
	$(UV) sync

sync:
	$(UV) sync --all-groups

test:
	$(UV) run pytest

test-verbose:
	$(UV) run pytest -v

test-unit:
	$(UV) run python -m unittest discover tests -v

test-service:
	cd backend && $(UV) run --locked pytest -q

run:
	$(UV) run streamlit run frontend/assistant_demo.py

clean:
	rm -rf .venv .pytest_cache __pycache__ */__pycache__ */*/__pycache__

dev-agentscope:
	./scripts/start_agentscope_dev.sh

stop-agentscope:
	./scripts/stop_agentscope_dev.sh

test-flow:
	$(UV) run ./scripts/test_agentscope_flow.py
