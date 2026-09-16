.PHONY: install test test-verbose run clean sync help

UV ?= uv

help:
	@echo "GenSlide development commands (powered by uv):"
	@echo "  make install     - Create virtual environment and install dependencies via uv sync"
	@echo "  make sync        - Sync all dependencies including dev tools"
	@echo "  make test        - Run tests using pytest via uv"
	@echo "  make test-verbose- Run tests in verbose mode"
	@echo "  make test-unit   - Run tests using built-in unittest via uv"
	@echo "  make run         - Run Streamlit application"
	@echo "  make clean       - Remove cached files and virtual environment"

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

run:
	$(UV) run streamlit run frontend/app.py

clean:
	rm -rf .venv .pytest_cache __pycache__ */__pycache__ */*/__pycache__
