# Makefile for REPL MCP Server

# Detect python and pytest in venv
PYTHON := $(shell test -f .venv/bin/python && echo .venv/bin/python || echo python3)
PYTEST := $(shell test -f .venv/bin/pytest && echo .venv/bin/pytest || echo pytest)
PIP := $(shell test -f .venv/bin/pip && echo .venv/bin/pip || echo pip)

.PHONY: help install test test-unit test-integration test-http test-examples lint clean verify

help:
	@echo "REPL MCP Server - Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install          Install dependencies"
	@echo ""
	@echo "Testing:"
	@echo "  make test             Run all tests"
	@echo "  make test-unit        Run unit tests only"
	@echo "  make test-integration Run integration tests"
	@echo "  make test-http        Run HTTP server tests"
	@echo "  make test-examples    Run example scripts"
	@echo ""
	@echo "Quality:"
	@echo "  make lint             Run linters"
	@echo "  make verify           Full verification (tests + examples)"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean            Remove build artifacts"

install:
	$(PYTHON) -m pip install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PYTEST) tests/ -v

test-unit:
	$(PYTEST) tests/test_models.py tests/test_repl_engine.py tests/test_server_init.py -v

test-integration:
	$(PYTEST) tests/test_integration.py -v

test-http:
	$(PYTEST) tests/test_http_integration.py -v

test-examples:
	@for script in examples/*.py; do \
		echo "Running $$script..."; \
		$(PYTHON) "$$script" || exit 1; \
	done

lint:
	@$(PIP) install ruff 2>/dev/null || true
	@ruff check . || true
	@echo "Lint check complete"

verify: test test-examples
	@echo ""
	@echo "✓ All tests passed!"
	@echo "✓ All examples passed!"
	@echo ""
	@echo "Server is ready to use!"

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache
	rm -rf .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
